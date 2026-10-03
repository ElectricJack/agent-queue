"""Request-scoped git delivery evidence, shared by publication and admission.

This module observes git; it never persists a delivery answer. Callers gather
task/completion identities outside row locks, then recheck those identities and
graph inputs plus ``snapshot.is_fresh`` before a delivery-sensitive mutation.
A leaf completion generation's exact source is located only by the immutable record
retained in git (:mod:`src.integration.provenance`). A verified parent's durable
operation completion locates its exact source without a leaf close record.
A leaf generation without retained provenance is
unlabelled: no branch head, reported commit or historical manifest stands in
for it, so it is unknown until an operator retains it
(``aq integration migrate-provenance``). An absent ref is never an empty artifact.

A *settlement* is the one database answer the evaluator honours, and it is not
a delivery: it records that a generation is **not owed** to one target (work
already delivered to the target a retarget replaced, a repair built for another
target, or an operator's ``aq integration settle-parked``). Git is asked first:
a settlement only turns *pending* into *settled*, never contained or unknown
work. It is fenced to the repository, the target and, for ordinary work, the
exact completion generation it settled, so a reopened task owes its new work.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from src.git.manager import GitError, GitManager, RemoteRefState, is_valid_git_oid
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

#: A generation that recorded an artifact (a branch or reported commits) but
#: has no exact source retained in git. Unknown, never delivered or empty.
MISSING_PROVENANCE = "missing_git_provenance"
INVALID_PARENT_COMPLETION = "invalid_parent_completion"

#: Task metadata recording that a generation's delivery is not owed to one
#: target (see the module docstring). ``completion_id`` ``None`` settles every
#: generation of the task; only a development repair, whose purpose is fixed
#: when it is filed, is settled that way.
SETTLEMENT_KEY = "development_delivery_settlement"


class DeliveryState(StrEnum):
    CONTAINED = "contained"
    NO_ARTIFACT = "no_artifact"
    PENDING = "pending"
    SETTLED = "settled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class VerifiedParentCompletion:
    """The durable parent close, never a fabricated leaf completion record.

    The verification id is immutable and globally unique. The other fields
    fence its current checkpoint/episode/operation binding on every recheck.
    """

    project_id: str
    repository_id: str
    branch: str
    episode_id: str
    operation_id: str
    verification_id: str
    generation: int
    source_oid: str
    completed_at: float
    checkpoint_version: int

    @property
    def completion_id(self):
        return "parent:" + self.verification_id


@dataclass(frozen=True)
class DeliveryRequest:
    """Immutable current task/completion inputs, including archived identities.

    ``task_version`` fences pre-provenance tasks without a completion record.
    An empty branchless identity is organizational; recorded code takes
    precedence even when its branch has subsequently been deleted.
    """

    project_id: str
    repository_id: str
    target_ref: str
    task_id: str
    task_version: float
    branch_name: str | None = None
    completion_id: str | None = None
    completed_at: float | None = None
    reported_source: str | None = None
    has_recorded_source: bool = False
    archived: bool = False
    task_status: str = "COMPLETED"
    claim_epoch: int = 0
    requires_parent_completion: bool = False
    parent_completion: VerifiedParentCompletion | None = None
    #: The recorded settlement (:data:`SETTLEMENT_KEY`), whatever it fences;
    #: :meth:`settles` decides whether it answers this request.
    settled_repository_id: str | None = None
    settled_target_ref: str | None = None
    settled_completion_id: str | None = None
    settled_reason: str | None = None

    @property
    def settles(self):
        """Whether the recorded settlement covers this target and generation."""
        return bool(
            self.settled_reason and self.settled_target_ref == self.target_ref
            and self.settled_repository_id == self.repository_id
            and self.settled_completion_id in {None, self.completion_id}
        )

    @classmethod
    def from_task(cls, task, completion, *, repository_id, target_ref):
        return cls(
            project_id=task["project_id"], repository_id=task.get("repo_id") or repository_id,
            target_ref=target_ref, task_id=task["id"], task_version=task["updated_at"],
            branch_name=task.get("branch_name"), archived=task.get("archived", False),
            completion_id=completion.id if completion else None,
            completed_at=completion.completed_at if completion else None,
            reported_source=completion.commits[-1] if completion and completion.commits else None,
            has_recorded_source=bool(completion and completion.commits),
            task_status=task["status"], claim_epoch=task.get("claim_epoch", 0),
        )


@dataclass(frozen=True)
class DeliveryEvidence:
    request: DeliveryRequest
    state: DeliveryState
    target_oid: str | None
    source_oid: str | None
    reason: str

    @property
    def satisfied(self):
        return self.state in {
            DeliveryState.CONTAINED, DeliveryState.NO_ARTIFACT, DeliveryState.SETTLED,
        }


@dataclass(frozen=True)
class DeliverySnapshot:
    """One fetched repository/target with a cache confined to this request.

    Reuse only for identical task/completion inputs and the exact target OID.
    No cache survives a new snapshot or restart. ``matches`` checks generation
    and caller-owned graph inputs; ``is_fresh`` observes the remote target
    without moving this snapshot's refs. A mismatch means retry, never delivery.
    """

    git: GitManager
    store: str
    project_id: str
    repository_id: str
    repository_url: str
    target_ref: str
    target_oid: str | None
    source_heads: Mapping[str, str]
    error: str | None = None
    _cache: dict[DeliveryRequest, DeliveryEvidence] = field(default_factory=dict, repr=False)
    _identities: dict[str, DeliveryRequest] = field(default_factory=dict, repr=False)

    def for_request(self):
        """The same fetched observation without another request's cached answers."""
        return replace(self, _cache={}, _identities={})

    def matches(self, requests, *, graph_inputs=None, current_graph_inputs=None):
        """Caller must reload the same task set and relevant graph inputs."""
        return set(requests) == set(self._cache) and graph_inputs == current_graph_inputs

    async def is_fresh(self, *, repository_url=None, target_ref=None):
        if self.error or not self.target_oid:
            return False
        if repository_url is not None and repository_url != self.repository_url:
            return False
        if target_ref is not None and target_ref != self.target_ref:
            return False
        try:
            origin = await _run(self.git, self.store, "remote", "get-url", "origin")
            if origin != self.repository_url:
                return False
            current = await self.git.als_remote_ref(
                self.store, self.target_ref.removeprefix("refs/heads/")
            )
            return current.state is RemoteRefState.PRESENT and current.oid == self.target_oid
        except (GitError, OSError):
            return False

    async def evaluate_many(self, requests):
        """Evaluate independently: one broken identity cannot poison peers."""
        requests = tuple(requests)
        if any(self._identities.get(request.task_id, request) != request for request in requests):
            self._cache.clear()
        self._identities.update({request.task_id: request for request in requests})
        return {request.task_id: await self.evaluate(request) for request in requests}

    async def evaluate(self, request):
        if self._identities.get(request.task_id, request) != request:
            self._cache.clear()
        self._identities[request.task_id] = request
        if request in self._cache:
            return self._cache[request]

        def result(state, reason, source=None):
            evidence = DeliveryEvidence(request, state, self.target_oid, source, reason)
            self._cache[request] = evidence
            return evidence

        if (request.project_id, request.repository_id, request.target_ref) != (
            self.project_id, self.repository_id, self.target_ref
        ):
            return result(DeliveryState.UNKNOWN, "scope_mismatch")
        if self.error or not self.target_oid:
            return result(DeliveryState.UNKNOWN, self.error or "missing_target")
        if request.requires_parent_completion and request.parent_completion is None:
            return result(DeliveryState.UNKNOWN, INVALID_PARENT_COMPLETION)
        try:
            provenance = GitProvenance(self.git, self.store, repository_url=self.repository_url)
            if request.completion_id:
                identity = CompletionIdentity(
                    request.project_id, request.repository_id, request.task_id,
                    request.completion_id,
                )
                record = await provenance.read_completion(identity)
                if request.parent_completion is not None:
                    source = request.parent_completion.source_oid
                    await provenance.exact(source)
                    if record is not None and (
                        record["source_oid"] != source or not record["artifact"]
                    ):
                        return result(DeliveryState.UNKNOWN, "parent_provenance_mismatch")
                    # The durable verified operation already locates the exact
                    # source. A retained Git record additionally permits an
                    # explicit replacement, using the ordinary provenance rules.
                    contained = await provenance.contained(
                        CompletedSource(identity, source), self.target_oid
                    ) if record is not None else await provenance.ancestor(source, self.target_oid)
                    if contained:
                        return result(DeliveryState.CONTAINED, "verified_parent_completion", source)
                    if request.settles:
                        return result(DeliveryState.SETTLED,
                                      "settled: " + request.settled_reason, source)
                    return result(DeliveryState.PENDING, "verified_parent_completion", source)
                if record is not None:
                    source = record["source_oid"]
                    # The immutable generation, rather than a branch tip or an
                    # arbitrary task trailer, identifies the complete artifact.
                    if not record["artifact"]:
                        return result(DeliveryState.NO_ARTIFACT, "git_no_artifact", source)
                    if await provenance.contained(CompletedSource(identity, source), self.target_oid):
                        return result(DeliveryState.CONTAINED, "git_completion", source)
                    if request.settles:
                        # Git first: a settlement only answers work the target
                        # lacks, and says it is not owed, never delivered.
                        return result(DeliveryState.SETTLED,
                                      "settled: " + request.settled_reason, source)
                    return result(DeliveryState.PENDING, "git_completion", source)
            # Without its retained record, a branch head, a reported commit or
            # a historical manifest could at best locate *a* commit, never the
            # complete final artifact of this generation.
            if request.branch_name or request.has_recorded_source:
                return result(DeliveryState.UNKNOWN, MISSING_PROVENANCE)
            return result(DeliveryState.NO_ARTIFACT, "branchless_organization")
        except (GitError, OSError, ValueError, KeyError, TypeError):
            return result(DeliveryState.UNKNOWN, "missing_or_ambiguous_source")

    @property
    def unlabelled_inventory(self):
        """Evaluated generations with an artifact but no exact source in git."""
        return tuple(sorted((
            (request.task_id, request.completion_id)
            for request, evidence in self._cache.items()
            if evidence.reason == MISSING_PROVENANCE
        ), key=lambda item: (item[0], item[1] or "")))


async def _run(git, store, *args):
    result = await git.arun_git_result(list(args), cwd=str(store))
    if result.returncode:
        raise GitError(result.stderr or result.stdout or "git observation failed")
    return result.stdout.strip()


async def load_delivery_requests(db, task_ids, *, repository_id, target_ref, conn=None):
    """Batch read current completions and live/archive identities without locks.

    Absent tasks are omitted so consumers explicitly withhold them. PostgreSQL
    DISTINCT ON gives a deterministic latest generation even at equal times.
    A guarded consumer passes its own *conn* to recheck, under its locks, that
    the identities it evaluated before the transaction are still current.
    """
    from contextlib import nullcontext

    from sqlalchemy import and_, select

    from src.database.queries.task_queries import (
        DEVELOPMENT_COMPLETION_ID_KEY, INTEGRATION_REWORK_AT_KEY,
    )
    from src.database.tables import archived_tasks, task_completion_records, task_metadata, tasks
    from src.integration.publishable_artifact import legacy_artifact

    task_ids = set(task_ids)
    opened = nullcontext(conn) if conn is not None else db._engine.connect()
    async with opened as reader:
        live = (
            await reader.execute(
                select(tasks, task_metadata.c.value.label("current_generation"))
                .select_from(tasks.outerjoin(task_metadata, and_(
                    task_metadata.c.task_id == tasks.c.id,
                    task_metadata.c.key == DEVELOPMENT_COMPLETION_ID_KEY,
                )))
                .where(tasks.c.id.in_(task_ids))
            )
        ).mappings().all()
        missing = task_ids - {row["id"] for row in live}
        archived = (await reader.execute(
            select(archived_tasks).where(archived_tasks.c.id.in_(missing))
        )).mappings().all() if missing else []
        completions = (await reader.execute(
            select(task_completion_records)
            .where(task_completion_records.c.task_id.in_(task_ids))
            .distinct(task_completion_records.c.task_id)
            .order_by(task_completion_records.c.task_id,
                      task_completion_records.c.completed_at.desc(),
                      task_completion_records.c.id.desc())
        )).mappings().all()
        recorded_ids = set((await reader.execute(
            select(task_completion_records.c.task_id).where(
                task_completion_records.c.task_id.in_(task_ids),
                task_completion_records.c.commits != "[]",
            )
        )).scalars())
        # A retired historical manifest named a source this generation never
        # recorded; its provenance stays unknown rather than organizational.
        recorded_ids |= set((await reader.execute(
            select(tasks.c.id).where(tasks.c.id.in_(task_ids), legacy_artifact(tasks))
        )).scalars())
        settlements = {
            task_id: settlement_fields(value)
            for task_id, value in (await reader.execute(
                select(task_metadata.c.task_id, task_metadata.c.value).where(
                    task_metadata.c.task_id.in_(task_ids),
                    task_metadata.c.key == SETTLEMENT_KEY,
                )
            )).all()
        }
        parent_ids, parent_completions = await _parent_completions_on(
            reader, task_ids, repository_id=repository_id,
        )
        rework = dict((await reader.execute(select(
            task_metadata.c.task_id, task_metadata.c.value,
        ).where(task_metadata.c.task_id.in_(parent_ids),
                task_metadata.c.key == INTEGRATION_REWORK_AT_KEY))).all()) if parent_ids else {}
    completion_by_id = {
        row["task_id"]: db._row_to_task_completion(row) for row in completions
    }
    requests = {}
    for rows, is_archived in ((live, False), (archived, True)):
        for row in rows:
            request = DeliveryRequest.from_task(
                {**row, "archived": is_archived}, completion_by_id.get(row["id"]),
                repository_id=repository_id, target_ref=target_ref,
            )
            current_id = json.loads(row["current_generation"]) if not is_archived and row[
                "current_generation"
            ] is not None else None
            if current_id and current_id != request.completion_id:
                # The transition committed but its descriptive completion row
                # has not: never evaluate an older, perhaps already contained,
                # generation for this newly completed incarnation.
                request = replace(
                    request, completion_id=current_id, completed_at=None,
                    reported_source=None,
                )
            if row["id"] in parent_ids:
                parent = parent_completions.get(row["id"])
                try:
                    rework_at = json.loads(rework.get(row["id"], "0"))
                    valid_rework = type(rework_at) in (int, float) and (
                        parent is not None and rework_at <= parent.completed_at
                    )
                except (ValueError, TypeError):
                    valid_rework = False
                # A newer ordinary close cannot borrow the old parent proof.
                if parent is not None and (
                    request.task_status != "COMPLETED"
                    or bool(current_id) or not valid_rework
                    or row.get("repo_id") != parent.repository_id
                    or (parent.project_id, parent.repository_id) != (
                        request.project_id, request.repository_id,
                    )
                    or parent.branch.removeprefix("refs/heads/") != (
                        request.branch_name or ""
                    ).removeprefix("refs/heads/")
                    or (request.completed_at is not None
                        and request.completed_at > parent.completed_at)
                ):
                    parent = None
                request = replace(
                    request, requires_parent_completion=True, parent_completion=parent,
                    completion_id=parent.completion_id if parent else None,
                    completed_at=parent.completed_at if parent else None,
                    reported_source=parent.source_oid if parent else None,
                )
            requests[row["id"]] = replace(
                request,
                has_recorded_source=(row["id"] in recorded_ids or bool(current_id)),
                **settlements.get(row["id"], {}),
            )
    return requests


async def _parent_completions_on(conn, task_ids, *, repository_id):
    """Current exact parent identities, including invalid bindings to fail closed.

    Operation completion is the authority that verification passed. This read
    neither reconstructs CI evidence nor treats an unfinished verification as
    a close. Episode generations may advance during collection; verification
    must match the current checkpoint generation, not the episode's initial one.
    """
    from sqlalchemy import and_, or_, select

    from src.database.tables import (
        integration_parent_episodes as episode,
        integration_parent_operation_completions as completion,
        integration_parent_verifications as verification,
        integration_repair_operations as operation,
        projects,
        repos,
        task_integration_checkpoints as checkpoint,
    )

    parent_ids = set((await conn.execute(select(checkpoint.c.task_id).where(
        checkpoint.c.task_id.in_(task_ids),
        or_(checkpoint.c.episode_id.is_not(None),
            checkpoint.c.last_completed_operation_id.is_not(None),
            checkpoint.c.current_verification_id.is_not(None)),
    ).union(select(completion.c.parent_task_id).where(
        completion.c.parent_task_id.in_(task_ids),
    )))).scalars())
    if not parent_ids:
        return parent_ids, {}
    rows = (await conn.execute(select(
        checkpoint.c.task_id, checkpoint.c.branch, checkpoint.c.repository_id,
        checkpoint.c.generation, checkpoint.c.version, checkpoint.c.verified_sha,
        episode.c.id.label("episode_id"), repos.c.project_id,
        operation.c.id.label("operation_id"), verification.c.id.label("verification_id"),
        completion.c.completed_at,
    ).select_from(
        checkpoint.join(episode, and_(
            episode.c.id == checkpoint.c.episode_id,
            episode.c.parent_task_id == checkpoint.c.task_id,
            episode.c.repository_id == checkpoint.c.repository_id,
        )).join(completion, and_(
            completion.c.operation_id == checkpoint.c.last_completed_operation_id,
            completion.c.verification_id == checkpoint.c.last_completed_verification_id,
            completion.c.parent_task_id == checkpoint.c.task_id,
            completion.c.episode_id == episode.c.id,
        )).join(verification, and_(
            verification.c.id == completion.c.verification_id,
            verification.c.operation_id == completion.c.operation_id,
            verification.c.parent_task_id == completion.c.parent_task_id,
            verification.c.episode_id == episode.c.id,
        )).join(operation, and_(
            operation.c.id == completion.c.operation_id,
            operation.c.parent_task_id == checkpoint.c.task_id,
            operation.c.episode_id == episode.c.id,
        )).join(repos, repos.c.id == checkpoint.c.repository_id)
         .join(projects, projects.c.id == repos.c.project_id)
    ).where(
        checkpoint.c.task_id.in_(parent_ids),
        checkpoint.c.repository_id == repository_id,
        projects.c.integration_repository_id == repository_id,
        checkpoint.c.verified_generation == checkpoint.c.generation,
        checkpoint.c.generation >= episode.c.generation,
        checkpoint.c.verified_sha == checkpoint.c.checkpoint_sha,
        checkpoint.c.current_verification_id == completion.c.verification_id,
        verification.c.generation == checkpoint.c.generation,
        verification.c.head_sha == checkpoint.c.verified_sha,
        verification.c.required_check_version == operation.c.required_check_version,
        operation.c.target_kind == "parent", operation.c.state == "completed",
    ))).mappings().all()
    return parent_ids, {
        row["task_id"]: VerifiedParentCompletion(
            row["project_id"], row["repository_id"], row["branch"],
            row["episode_id"], row["operation_id"], row["verification_id"],
            row["generation"], row["verified_sha"], row["completed_at"], row["version"],
        ) for row in rows
    }


def settlement_fields(value):
    """The :class:`DeliveryRequest` fields of one stored settlement.

    A malformed record settles nothing: the generation stays owed.
    """
    try:
        record = json.loads(value) if isinstance(value, str) else value
    except ValueError:
        return {}
    if not isinstance(record, dict) or not all(
        isinstance(record.get(key), str) and record[key]
        for key in ("repository_id", "target_ref", "reason")
    ):
        return {}
    completion = record.get("completion_id")
    if completion is not None and not (isinstance(completion, str) and completion):
        return {}  # never widen a damaged generation fence to every generation
    return {
        "settled_repository_id": record["repository_id"],
        "settled_target_ref": record["target_ref"],
        "settled_completion_id": completion,
        "settled_reason": record["reason"],
    }


async def delivery_snapshot(git, store, *, project_id, repository_id, repository_url,
                            target_ref):
    """Fetch once and pin all batch observations to its exact target OID.

    Git failure yields an unknown snapshot even if stale tracking refs exist.
    It is never converted to an empty source or a successful delivery.
    """
    target_oid, heads, error = None, {}, None
    try:
        if await _run(git, store, "remote", "get-url", "origin") != repository_url:
            raise GitError("repository_mismatch")
        if not target_ref.startswith("refs/heads/"):
            raise GitError("invalid_target")
        await _run(git, store, "check-ref-format", target_ref)
        await git.afetch_origin(str(store), repository_url=repository_url, all_heads=True)
        refs = await _run(
            git, store, "for-each-ref", "--format=%(refname) %(objectname)",
            "refs/remotes/origin/",
        )
        heads = dict(line.split(" ", 1) for line in refs.splitlines())
        target_oid = heads.get("refs/remotes/origin/" + target_ref.removeprefix("refs/heads/"))
        if not is_valid_git_oid(target_oid):
            error = "missing_target"
    except (GitError, OSError) as exc:
        error = f"snapshot_git_error: {exc}"
    return DeliverySnapshot(
        git, str(store), project_id, repository_id, repository_url, target_ref,
        target_oid, MappingProxyType(heads), error,
    )
