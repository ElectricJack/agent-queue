"""Request-scoped git delivery evidence, shared by publication and admission.

This module observes git; it never persists a delivery answer. Callers gather
task/completion identities outside row locks, then recheck those identities and
graph inputs plus ``snapshot.is_fresh`` before a delivery-sensitive mutation.
A completion generation's exact source is located only by the immutable record
retained in git (:mod:`src.integration.provenance`). A generation without one is
unlabelled: no branch head, reported commit or historical manifest stands in
for it, so it is unknown until an operator retains it
(``aq integration migrate-provenance``). An absent ref is never an empty artifact.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType

from src.git.manager import GitError, GitManager, RemoteRefState, is_valid_git_oid
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance

#: A generation that recorded an artifact (a branch or reported commits) but
#: has no exact source retained in git. Unknown, never delivered or empty.
MISSING_PROVENANCE = "missing_git_provenance"


class DeliveryState(StrEnum):
    CONTAINED = "contained"
    NO_ARTIFACT = "no_artifact"
    PENDING = "pending"
    UNKNOWN = "unknown"


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
        return self.state in {DeliveryState.CONTAINED, DeliveryState.NO_ARTIFACT}


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
        try:
            provenance = GitProvenance(self.git, self.store, repository_url=self.repository_url)
            if request.completion_id:
                identity = CompletionIdentity(
                    request.project_id, request.repository_id, request.task_id,
                    request.completion_id,
                )
                record = await provenance.read_completion(identity)
                if record is not None:
                    source = record["source_oid"]
                    # The immutable generation, rather than a branch tip or an
                    # arbitrary task trailer, identifies the complete artifact.
                    if not record["artifact"]:
                        return result(DeliveryState.NO_ARTIFACT, "git_no_artifact", source)
                    if await provenance.contained(CompletedSource(identity, source), self.target_oid):
                        return result(DeliveryState.CONTAINED, "git_completion", source)
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

    from sqlalchemy import select

    from src.database.tables import archived_tasks, task_completion_records, tasks
    from src.integration.publishable_artifact import legacy_artifact

    task_ids = set(task_ids)
    opened = nullcontext(conn) if conn is not None else db._engine.connect()
    async with opened as reader:
        live = (
            await reader.execute(select(tasks).where(tasks.c.id.in_(task_ids)))
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
    completion_by_id = {
        row["task_id"]: db._row_to_task_completion(row) for row in completions
    }
    return {
        row["id"]: replace(
            DeliveryRequest.from_task(
                {**row, "archived": is_archived}, completion_by_id.get(row["id"]),
                repository_id=repository_id, target_ref=target_ref,
            ),
            has_recorded_source=row["id"] in recorded_ids,
        )
        for rows, is_archived in ((live, False), (archived, True)) for row in rows
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
