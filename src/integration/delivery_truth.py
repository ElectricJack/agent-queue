"""Request-scoped git delivery evidence, shared by publication and admission.

This module observes git; it never persists a delivery answer. Callers gather
task/completion identities outside row locks, then recheck those identities and
graph inputs plus ``snapshot.is_fresh`` before a delivery-sensitive mutation.
Legacy source locators are an explicit bridge for operations/retire to remove
once exact completion provenance is retained in git. Receipt *state* is never
proof, and an absent ref is never an empty artifact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping

from src.git.manager import GitError, GitManager, RemoteRefState, is_valid_git_oid


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
    legacy_rows: tuple = ()
    _cache: dict[DeliveryRequest, DeliveryEvidence] = field(default_factory=dict, repr=False)
    _identities: dict[str, DeliveryRequest] = field(default_factory=dict, repr=False)

    def with_legacy_rows(self, rows):
        """Attach a source-location inventory without retaining cached answers."""
        return replace(self, legacy_rows=tuple(rows), _cache={}, _identities={})

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
            source = request.reported_source
            locator = "completion"
            if source is not None:
                # Resolve a unique legacy abbreviation, but never accept a ref
                # name, revision expression, missing object or old generation.
                if not isinstance(source, str) or not re.fullmatch(r"[0-9a-f]{7,40}", source):
                    return result(DeliveryState.UNKNOWN, "invalid_completion_source")
                source = await _run(
                    self.git, self.store, "rev-parse", "--verify", "--end-of-options",
                    source + "^{commit}",
                )
            else:
                # TEMPORARY LEGACY BRIDGE: scope every locator to repository,
                # target and current completion. Operations migrates these
                # mappings to git; retire removes this entire reader.
                located = self._legacy_sources(request, bound_only=True)
                if len(located) > 1:
                    return result(DeliveryState.UNKNOWN, "ambiguous_legacy_sources")
                source = next(iter(located), None)
                locator = "legacy_completion_source"
                if source is None and request.branch_name:
                    source = self.source_heads.get(
                        "refs/remotes/origin/" + request.branch_name.removeprefix("refs/heads/")
                    )
                    locator = "legacy_unlabelled_branch_head"
                if source is None:
                    located = self._legacy_sources(request)
                    if len(located) > 1:
                        return result(DeliveryState.UNKNOWN, "ambiguous_legacy_sources")
                    source = next(iter(located), None)
                    locator = "legacy_completion_source"
                if source is None:
                    if request.branch_name:
                        return result(DeliveryState.UNKNOWN, "missing_ref")
                    if request.has_recorded_source or any(
                        row.get("project_id") == request.project_id
                        and row.get("repository_id") == request.repository_id
                        and any(member.get("task_id") == request.task_id and
                                is_valid_git_oid(member.get("source_sha"))
                                for member in row.get("manifest", []))
                        for row in self.legacy_rows
                    ):
                        return result(DeliveryState.UNKNOWN, "unresolved_completion_source")
                    return result(DeliveryState.NO_ARTIFACT, "branchless_organization")
            if not is_valid_git_oid(source):
                return result(DeliveryState.UNKNOWN, "invalid_source")
            contained = await self.git.ais_ancestor(self.store, source, self.target_oid, strict=True)
            if contained is None:
                return result(DeliveryState.UNKNOWN, "git_error", source)
            if contained:
                return result(DeliveryState.CONTAINED, locator, source)
            if await self._legacy_replacement(request, source):
                return result(DeliveryState.CONTAINED, "legacy_repair_replacement", source)
            if await self._legacy_adoption(request, source):
                return result(DeliveryState.CONTAINED, "legacy_operator_adoption", source)
            return result(DeliveryState.PENDING, locator, source)
        except (GitError, OSError):
            return result(DeliveryState.UNKNOWN, "missing_or_ambiguous_source")

    async def _legacy_replacement(self, request, source):
        """TEMPORARY operations/retire bridge for exact validated repair maps.

        A resolved repair event identifies the original source and the exact
        replacement completion. Both generation fences and git ancestry of
        the replacement are required. Neither adopted state nor an arbitrary
        superseded_by member is accepted as equivalence evidence.
        """
        for row in self.legacy_rows:
            if (row.get("project_id"), row.get("repository_id"), row.get("target_ref")) != (
                request.project_id, request.repository_id, request.target_ref
            ):
                continue
            boundary = request.completed_at if request.completed_at is not None else (
                request.task_version
            )
            if float(row.get("created_at", 0)) < boundary or not any(
                member.get("task_id") == request.task_id and member.get("source_sha") == source
                for member in row.get("manifest", [])
            ):
                continue
            proof = (row.get("evidence") or {}).get("resolved_by_delivered_repair") or {}
            repair = self._identities.get(proof.get("task_id"))
            head = proof.get("accepted_head")
            if (repair is None or repair.task_status != "COMPLETED"
                    or repair.completion_id != proof.get("completion_id")
                    or (repair.project_id, repair.repository_id, repair.target_ref) != (
                        request.project_id, request.repository_id, request.target_ref
                    ) or not is_valid_git_oid(repair.reported_source)
                    or not is_valid_git_oid(head) or row.get("prepared_sha") != head):
                continue
            # The recovery writer already checked the exact repair contract;
            # independently prove its current final source remains on our target.
            if await self.git.ais_ancestor(
                self.store, repair.reported_source, head, strict=True
            ) is True and await self.git.ais_ancestor(
                self.store, head, self.target_oid, strict=True
            ) is True:
                return True
        return False

    async def _legacy_adoption(self, request, source):
        """TEMPORARY operations/retire bridge for an explicit operator adoption.

        ``integration adopt`` records an operator's decision that this exact
        source is delivered as the adopted head: by ancestry, or by explicit
        equivalence when the content landed rebased. Only that operator record
        counts (a bare adopted state does not), only for this completion
        generation, and only while git still contains the adopted head.
        """
        for row in self.legacy_rows:
            evidence = row.get("evidence") or {}
            if row.get("state") != "adopted" or evidence.get("kind") != "operator_accepted":
                continue
            if (row.get("project_id"), row.get("repository_id"), row.get("target_ref")) != (
                request.project_id, request.repository_id, request.target_ref
            ):
                continue
            boundary = request.completed_at if request.completed_at is not None else (
                request.task_version
            )
            if float(row.get("created_at", 0)) < boundary or not any(
                member.get("task_id") == request.task_id and member.get("source_sha") == source
                for member in row.get("manifest", [])
            ):
                continue
            bound = {proof.get("completion_id") for proof in evidence.get("completion_sources", [])
                     if proof.get("task_id") == request.task_id}
            if bound and request.completion_id not in bound:
                continue
            head = row.get("prepared_sha")
            if is_valid_git_oid(head) and await self.git.ais_ancestor(
                self.store, head, self.target_oid, strict=True
            ) is True:
                return True
        return False

    def _legacy_sources(self, request, *, bound_only=False):
        sources = set()
        for row in self.legacy_rows:
            if (row.get("project_id"), row.get("repository_id")) != (
                request.project_id, request.repository_id
            ):
                continue
            # Historical assembly refs locate sources too. They never establish
            # containment: every located source is tested against our exact
            # configured target, rather than the assembly target or row state.
            row_target = row.get("target_ref") or ""
            if row_target != request.target_ref and not row_target.startswith(
                "refs/heads/aq/development/"
            ):
                continue
            proofs = (row.get("evidence") or {}).get("completion_sources", [])
            exact = [proof.get("source_sha") for proof in proofs if (
                proof.get("task_id") == request.task_id
                and request.completion_id is not None
                and proof.get("completion_id") == request.completion_id
            )]
            if exact:
                sources.update(source for source in exact if is_valid_git_oid(source))
                continue
            if bound_only:
                continue
            # Unlabelled manifest fallback has a bounded generation fence.
            # It is an inventory item, not delivery authority. Never borrow a
            # manifest explicitly bound to a different completion generation.
            if any(proof.get("task_id") == request.task_id for proof in proofs):
                continue
            boundary = request.completed_at if request.completed_at is not None else (
                request.task_version
            )
            if float(row.get("created_at", 0)) < boundary:
                continue
            sources.update(member["source_sha"] for member in row.get("manifest", []) if (
                member.get("task_id") == request.task_id
                and is_valid_git_oid(member.get("source_sha"))
            ))
        return sources

    @property
    def legacy_inventory(self):
        """Exact identities operations must migrate before bridge retirement."""
        return tuple(sorted((
            (request.task_id, request.completion_id, evidence.source_oid, evidence.reason)
            for request, evidence in self._cache.items()
            if evidence.reason.startswith("legacy_")
        ), key=lambda item: (item[0], item[1] or "")))


async def _run(git, store, *args):
    result = await git.arun_git_result(list(args), cwd=str(store))
    if result.returncode:
        raise GitError(result.stderr or result.stdout or "git observation failed")
    return result.stdout.strip()


async def load_delivery_requests(db, task_ids, *, repository_id, target_ref):
    """Batch read current completions and live/archive identities without locks.

    Absent tasks are omitted so consumers explicitly withhold them. PostgreSQL
    DISTINCT ON gives a deterministic latest generation even at equal times.
    """
    from sqlalchemy import select

    from src.database.tables import archived_tasks, task_completion_records, tasks

    task_ids = set(task_ids)
    async with db._engine.connect() as conn:
        live = (await conn.execute(select(tasks).where(tasks.c.id.in_(task_ids)))).mappings().all()
        missing = task_ids - {row["id"] for row in live}
        archived = (await conn.execute(
            select(archived_tasks).where(archived_tasks.c.id.in_(missing))
        )).mappings().all() if missing else []
        completions = (await conn.execute(
            select(task_completion_records)
            .where(task_completion_records.c.task_id.in_(task_ids))
            .distinct(task_completion_records.c.task_id)
            .order_by(task_completion_records.c.task_id,
                      task_completion_records.c.completed_at.desc(),
                      task_completion_records.c.id.desc())
        )).mappings().all()
        recorded_ids = set((await conn.execute(
            select(task_completion_records.c.task_id).where(
                task_completion_records.c.task_id.in_(task_ids),
                task_completion_records.c.commits != "[]",
            )
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
                            target_ref, legacy_rows=()):
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
        target_oid, MappingProxyType(heads), error, tuple(legacy_rows),
    )
