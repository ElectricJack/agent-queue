"""Database and daemon sources for the reduced integration train.

Everything here is read from durable rows and Git at visit time: the targets a
project's completed work routes to, the exact completion sources not yet in
those targets, and the open batch's frozen inputs. Aborting a batch withholds
its exact (task, source) inputs until the task completes again. An explicit
ejection instruction releases those inputs for admission after replacing the
batch with its remaining members.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, replace
from typing import Any

import yaml
from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from src.database.tables import (
    archived_tasks,
    integration_batch_members,
    integration_batches,
    integration_check_evidence,
    integration_legacy_deliveries,
    integration_review_evidence,
    integration_source_ci,
    projects,
    repos,
    task_branch_origins,
    task_completion_records,
    task_dependencies,
    task_metadata,
    tasks,
)
from src.git.github_contracts import GitHubAccessError
from src.git.manager import GitError, is_valid_git_oid
from src.integration.batches import (
    Batch,
    BatchMember,
    BatchObservation,
    BatchService,
    BatchStore,
    SupersedeMemberUnavailable,
    candidate_ref,
    ejection_instruction,
)
from src.integration.candidate_baseline import CandidateBaselineService
from src.integration.ci import (
    AttestationError,
    IntegrationTrustManifest,
    SubjectTrustError,
    select_trusted_attestation,
)
from src.integration.delivery_observer import DeliveryTarget, delivery_targets
from src.integration.delivery_truth import (
    DeliveryRequest,
    DeliveryState,
    load_delivery_requests,
    superseded_source_repairs_on,
    undeliverable_repairs_on,
)
from src.integration.epics import EpicGraphReader, EpicPolicy, EpicReadinessEvaluator, HeadChecks
from src.integration.git_truth import GitTruth, GitTruthSnapshot
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.lock import BranchLock
from src.integration.models import (
    BranchKey, IntegrationTrainPolicy, RepairPolicy, integration_ci_policy,
)
from src.integration.promotion_steps import flow_status, flow_targets
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.regeneration import DEFAULT_REGENERATE_COMMAND
from src.integration.reviews import ReviewRequirements, ReviewSubject, TreeReviews
from src.integration.selection_metrics import selection_count, selection_stage
from src.integration.runtime_contracts import Subject
from src.integration.train import (
    BatchSelection,
    CandidateChecks,
    IntegrationTrain,
    TrainLane,
    TrainTarget,
)
from src.projects.github import GitHubError

logger = logging.getLogger(__name__)

TRAIN_MODES = ("train", "hierarchy", "development")
TRAIN_HOLDER = "service:integration-train"
# A local branch keeps a candidate reachable from the retained store, so a
# detached validation clone sees it. Never pushed.
RETAINED_CANDIDATE_PREFIX = "refs/heads/aq/train-candidate/"
MEMBER_LIMIT = 200
#: A promotion lane is rebuilt on every visit; its subject trust and App client
#: are kept per batch this long after last use, so a held request does not
#: refetch S's manifest each time. S is immutable; the key carries the policy.
PROMOTION_RESOLUTION_TTL_SECONDS = 900.0


async def _finish_on_cancel(operation):
    """Join a mutation even if its visit is cancelled; never orphan a pushed head."""
    writing = asyncio.create_task(operation)
    try:
        return await asyncio.shield(writing)
    except asyncio.CancelledError:
        # Shield repeatedly: shutdown can cancel a visit that is already
        # finishing a write after the visit deadline cancelled it once.
        while not writing.done():
            try:
                await asyncio.shield(writing)
            except asyncio.CancelledError:
                continue
        writing.result()
        raise


def _push_branch_allowed(push: dict, ref: str) -> bool | None:
    """Diagnose simple branch globs; unfamiliar syntax remains unknown.

    This is explanatory evidence only, never a substitute for a push run.
    """
    branch = ref.removeprefix("refs/heads/")
    if any(key in push and not isinstance(push[key], list)
           for key in ("branches", "branches-ignore")):
        return None

    def matches(pattern):
        if not isinstance(pattern, str) or re.search(r"[?+\[\]\\]", pattern):
            return None
        glob = re.escape(pattern).replace(r"\*\*", ".*").replace(r"\*", "[^/]*")
        return re.fullmatch(glob, branch) is not None

    allowed = "branches" not in push
    if allowed and ("tags" in push or "tags-ignore" in push) and "branches-ignore" not in push:
        return False
    for pattern in push.get("branches", []):
        negative = isinstance(pattern, str) and pattern.startswith("!")
        match = matches(pattern[1:] if negative else pattern)
        if match is None:
            return None
        if match:
            allowed = not negative
    for pattern in push.get("branches-ignore", []):
        match = matches(pattern)
        if match is None:
            return None
        if match:
            allowed = False
    return allowed


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
    from src.integration.promotion_routing import cutover_origin_clause

    flow, cutover = (await conn.execute(select(projects.c.promotion_flow,
        projects.c.default_branch_cutover).where(projects.c.id == project_id))).one()
    promotion_refs = {
        ref for name in flow_targets(flow) for ref in (name, _branch(name))
    }
    rows = await conn.execute(
        select(tasks.c.id)
        .select_from(tasks.join(task_branch_origins, and_(
            task_branch_origins.c.task_id == tasks.c.id,
            task_branch_origins.c.repository_id == repository_id,
            task_branch_origins.c.retired_at.is_(None),
        )))
        .where(tasks.c.project_id == project_id, tasks.c.status == "COMPLETED",
               or_(tasks.c.task_type.is_(None), tasks.c.task_type != "promotion"),
               or_(task_branch_origins.c.parent_ref.is_(None),
                   task_branch_origins.c.parent_ref.not_in(promotion_refs),
                   cutover_origin_clause(cutover, repository_id)))
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


def _origin_heads(observed, origins) -> dict[str, str | None]:
    """Each live origin's fetched branch head, from the visit's pinned refs."""
    heads = {}
    for origin in origins:
        head = observed.source_heads.get(
            "refs/remotes/origin/" + origin["branch_name"].removeprefix("refs/heads/")
        ) if origin["branch_name"] else None
        heads[origin["task_id"]] = head if is_valid_git_oid(head or "") else None
    return heads

def _stack_snapshot_digest(observed, origins, delivered) -> str:
    """One digest of everything a containment answer depends on.

    Cached answers are keyed by it, so a reused answer is only ever the answer
    the same base, target head, ref snapshot, live origins and delivered set
    would give again.
    """
    digest = hashlib.sha256()
    for ref, oid in sorted(observed.source_heads.items()):
        digest.update(f"{ref}={oid}\n".encode())
    for origin in sorted(origins, key=lambda row: row["task_id"]):
        digest.update(
            f"{origin['task_id']}={origin['branch_name']}={origin['base_sha']}\n".encode()
        )
    digest.update(("delivered=" + ",".join(sorted(delivered))).encode())
    return digest.hexdigest()

def _reaches(edges, start, goal) -> bool:
    """Whether declared prerequisites already order *start* after *goal*."""
    remaining, seen = [start], {start}
    while remaining:
        current = remaining.pop()
        if current == goal:
            return True
        for need in edges.get(current, ()):
            if need not in seen:
                seen.add(need)
                remaining.append(need)
    return False

async def _source_ci_prerequisites_on(conn, ids, bases, *, repository_id) -> dict[str, set[str]]:
    """Repair delegates whose recorded base is their bound source's exact head.

    A source-CI repair must merge the exact red source head above its own base,
    so its origin records that head as the base: a stack the daemon itself
    declared. Reporting it as an undeclared stack would refuse a repair of a
    source that cannot go green until the repair lands, which is the deadlock
    the delivery order below exists to prevent. Only a binding whose recorded
    base is that exact head counts, and only for this repository.
    """
    candidates = {task_id for task_id in ids if task_id in bases}
    if not candidates:
        return {}
    rows = (await conn.execute(select(
        integration_source_ci.c.repair_task_id, integration_source_ci.c.task_id,
        integration_source_ci.c.source_head, integration_source_ci.c.source_base,
    ).where(
        integration_source_ci.c.repository_id == repository_id,
        integration_source_ci.c.repair_task_id.in_(candidates),
        integration_source_ci.c.repair_task_id.is_not(None),
    ))).all()
    bindings: dict[str, set[str]] = {}
    for repair_id, source_id, source_head, source_base in rows:
        if (source_id != repair_id and bases.get(repair_id or "") == source_head
                and bases.get(source_id) == source_base):
            bindings.setdefault(repair_id, set()).add(source_id)
    return bindings

async def _epic_branches_on(conn, ids):
    """Keep closed containers visitable even after their child origins retire."""
    from src.database.queries.hierarchy_queries import container_flag_exists

    child, archived = tasks.alias("epic_child"), archived_tasks.alias("epic_archived_child")
    return dict((await conn.execute(select(tasks.c.id, tasks.c.branch_name).where(
        tasks.c.id.in_(ids), tasks.c.branch_name.is_not(None),
        container_flag_exists() | select(child.c.id).where(
            child.c.parent_task_id == tasks.c.id).exists() | select(archived.c.id).where(
            archived.c.parent_task_id == tasks.c.id).exists(),
    ))).all())


async def project_snapshot(db, target: TrainTarget) -> GitTruthSnapshot | None:
    """Observe the project root through the daemon's isolated delivery store."""
    from src.integration.delivery_observer import prerequisite_observer

    observer = prerequisite_observer(db) or getattr(db, "_delivery_observer", None)
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
            record = await snapshot.read_completion(CompletionIdentity(
                project_id, repository_id, task_id,
                request.completion_id or request.legacy_generation,
            ))
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
                 probe_timeout_seconds: float = 5.0, flow_problems=None) -> None:
        self.db, self.limit = db, limit
        self.snapshot = snapshot
        self.probe_timeout_seconds = probe_timeout_seconds
        self.flow_problems = flow_problems

    async def targets(self, now: float) -> list[TrainTarget]:
        found: dict[tuple[str, str, str], TrainTarget] = {}
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(projects.c.id, projects.c.hierarchical_integration_mode,
                       projects.c.promotion_flow,
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
        recorded = self.flow_problems() if callable(self.flow_problems) else self.flow_problems
        status = flow_status(
            row["promotion_flow"], default_branch=row["default_branch"],
            recorded=(recorded or {}).get(project_id),
        )
        flow = row["promotion_flow"] if status and status["state"] == "configured" else []
        promotions = {
            _branch(step["target"]): TrainTarget(
                project_id, repository_id, _branch(step["target"]), "promotion", step=step,
            ) for step in flow
        }
        async with self.db._engine.connect() as conn:
            pending = await _pending_tasks(conn, project_id, repository_id, limit=None)
            routed = await delivery_targets(conn, pending, reduced=True)
            epics = await _epic_branches_on(conn, pending)
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
        refs.update(_branch(branch) for task_id, branch in epics.items() if task_id not in delivered)
        refs.update(promotions)
        return [root if ref == default else promotions.get(ref) or
                TrainTarget(project_id, repository_id, ref, "epic") for ref in sorted(refs)]


def _conflict_action(refusal, snapshot, *, epic: bool, refresh=None):
    """Name who resolves a root PR that conflicts with the default branch."""
    if refusal.get("code") != "pr_conflicting":
        return refusal
    action = "merge the default branch into the task branch, push it and close the task again"
    detail = {}
    if epic:
        action = "the train will try to refresh the epic from the default branch"
        if refresh is not None:
            action = {
                "started": "the train started an epic refresh from the default branch",
                "running": "an epic refresh is running on the epic branch",
                "pending": "another batch owns the epic; the train will retry its refresh "
                           "after that batch settles",
                "settled": "the refresh for this head pair has settled; the epic still needs "
                           "a conflict-free PR with green exact-head checks",
                "current": "the epic already contains the default branch; its PR still needs "
                           "green exact-head checks",
                "unavailable": f"the epic refresh is unavailable ({refresh.get('reason')}); "
                               "the train will retry",
            }[refresh["outcome"]]
            detail["refresh"] = refresh
            if refresh["outcome"] == "unavailable":
                detail.update(code="refresh_unavailable", retry_at=refresh["retry_at"],
                              retry_seconds=refresh["retry_seconds"])
    return {**refusal, **detail, "default_sha": snapshot.target_oid, "epic": epic, "action": action,
            "detail": f"root {refusal['task_id']} PR conflicts with the default branch at "
                      f"{snapshot.target_oid}: {action}"}
class BackmergeAdmission:
    """Daemon-authored immutable sources use the lower branch's ordinary PR gate."""

    def __init__(self, db, gitops, *, clock=time.time):
        self.db, self.gitops, self.clock = db, gitops, clock

    async def author(self, project_id, repository_id, origin_ref, target_ref, source, client,
                     *, enabled=True, validate_on=None):
        from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY
        from src.git.github import GitHubAccess
        from src.integration.ownership import BranchOwnership

        repo = await self.gitops.repository(Batch("backmerge", project_id, repository_id, target_ref))
        target = await self.gitops.remote(repo, target_ref)
        if not target:
            raise ValueError("backmerge target is missing")
        if await self.gitops.is_ancestor(repo, source, target):
            return None
        base = (await self.gitops.run(repo, "merge-base", source, target)).strip()
        key = f"backmerge:{repository_id}:{source}:{target_ref}"
        digest = hashlib.sha256(key.encode()).hexdigest()
        tid, branch = "backmerge-" + digest[:24], "aq/backmerge/" + digest
        generation = "backmerge:" + source + ":" + tid
        meta = {"source_sha": source, "base_sha": base, "origin_ref": origin_ref,
                "target_ref": target_ref, "kind": "source" if enabled else "ledger"}
        async with self.db.immediate() as conn:
            await conn.execute(select(func.pg_advisory_xact_lock(
                func.hashtext("aq-backmerge:" + repository_id))))
            if validate_on:
                await validate_on(conn)
            existing = (await conn.execute(select(tasks.c.pr_url, task_metadata.c.value)
                .join(task_metadata, task_metadata.c.task_id == tasks.c.id)
                .where(tasks.c.id == tid, task_metadata.c.key == "backmerge"))).first()
            if existing:
                previous = json.loads(existing.value)
                if previous["kind"] == "source" or not enabled:
                    return {"task_id": tid, "pr_url": existing.pr_url, **previous}
                # Enabling authorship for an existing ledger preserves the pinned base.
                meta = {**previous, "kind": "source"}
                base = meta["base_sha"]
            elif await conn.scalar(select(archived_tasks.c.id).where(archived_tasks.c.id == tid)):
                record = await conn.scalar(select(task_completion_records.c.notes)
                    .where(task_completion_records.c.id == generation))
                return {"task_id": tid, **json.loads(record)["backmerge"]}
            if not enabled:
                now = self.clock()
                await conn.execute(insert(tasks).values(
                    id=tid, project_id=project_id, repo_id=repository_id,
                    title=f"Back-merge owed from {origin_ref} to {target_ref}", description="",
                    task_type="backmerge", status="COMPLETED", created_by_kind="backmerge",
                    created_by_id=TRAIN_HOLDER, dedup_key=key, created_at=now, updated_at=now,
                ))
                await conn.execute(insert(task_metadata).values(
                    task_id=tid, key="backmerge", value=json.dumps(meta, sort_keys=True),
                ))
                await conn.execute(insert(task_completion_records).values(id=generation,
                    task_id=tid, outcome="pass", notes=json.dumps({"backmerge": meta}),
                    completed_at=now))
                return {"task_id": tid, **meta}
            # Frozen membership remains intact even when its originating branch moves again.
            old = (await conn.execute(select(tasks.c.id, task_metadata.c.value)
                .join(task_metadata, task_metadata.c.task_id == tasks.c.id)
                .where(tasks.c.repo_id == repository_id, tasks.c.task_type == "backmerge",
                       tasks.c.status == "COMPLETED", task_metadata.c.key == "backmerge")))
            for old_id, value in old:
                previous = json.loads(value)
                frozen = await conn.scalar(select(integration_batch_members.c.batch_id)
                    .join(integration_batches, integration_batches.c.id == integration_batch_members.c.batch_id)
                    .where(integration_batch_members.c.task_id == old_id,
                           integration_batches.c.intent != "aborted",
                           integration_batches.c.lifecycle != "promoted"))
                if (not frozen and previous["target_ref"] == target_ref
                        and previous["origin_ref"] == origin_ref
                        and old_id != tid
                        and await self.gitops.is_ancestor(repo, previous["source_sha"], source)):
                    await conn.execute(update(tasks).where(tasks.c.id == old_id)
                                       .values(status="FAILED", updated_at=self.clock()))
                    await conn.execute(insert(task_metadata).values(task_id=old_id,
                        key="backmerge_result", value=json.dumps({"reason": "superseded", "by": tid}))
                        .on_conflict_do_nothing(index_elements=["task_id", "key"]))
            await BranchOwnership(self.db).acquire(
                BranchKey(repository_id=repository_id, branch="refs/heads/" + branch),
                tid, "worker", conn=conn,
            )
        # Ref creation, completion retention and the PR are recoverable by their
        # deterministic identities. No provider request holds a DB transaction.
        remote = await self.gitops.remote(repo, "refs/heads/" + branch)
        if remote is None:
            await self.gitops.push(repo, "refs/heads/" + branch, source, "0" * 40)
        elif remote != source:
            raise ValueError("backmerge source ref changed")
        await GitProvenance(self.gitops.git, str(repo.store),
            repository_url=f"https://github.com/{repo.binding.full_name}.git").write_completion(
                CompletedSource(CompletionIdentity(project_id, repository_id, tid, generation), source))
        url = await client.create_pull_request(title=f"Back-merge {origin_ref}@{source[:12]} into {target_ref}",
            body=f"Back-merge `{source}` through this branch's train gate.\n\nAQ-Backmerge: {key}\n",
            head=branch, base=target_ref.removeprefix("refs/heads/"))
        GitHubAccess.validate_pr_url(repo.binding, url)
        if await self.gitops.remote(repo, origin_ref) != source:
            raise ValueError("backmerge originating head changed during authorship")
        async with self.db.immediate() as conn:
            await conn.execute(select(func.pg_advisory_xact_lock(
                func.hashtext("aq-backmerge:" + repository_id))))
            existing = (await conn.execute(select(tasks.c.pr_url, task_metadata.c.value)
                .join(task_metadata, task_metadata.c.task_id == tasks.c.id)
                .where(tasks.c.id == tid, task_metadata.c.key == "backmerge"))).first()
            if existing and json.loads(existing.value).get("kind") == "source":
                return {"task_id": tid, "pr_url": existing.pr_url, **json.loads(existing.value)}
            if validate_on:
                await validate_on(conn)
            now = self.clock()
            await conn.execute(insert(tasks).values(id=tid, project_id=project_id, repo_id=repository_id,
                title=f"Back-merge {origin_ref}@{source[:12]} into {target_ref}", description="Daemon-authored source.",
                task_type="backmerge", status="COMPLETED", branch_name=branch, pr_url=url,
                created_by_kind="backmerge", created_by_id="service:integration-train", dedup_key=key,
                created_at=now, updated_at=now).on_conflict_do_update(index_elements=["id"],
                    set_={"branch_name": branch, "pr_url": url, "updated_at": now}))
            await conn.execute(insert(task_branch_origins).values(id="origin-" + digest, task_id=tid,
                repository_id=repository_id, branch_name=branch, base_sha=base, creation_generation=0,
                reserved=True, materialized=True, created_at=now, materialized_at=now))
            await conn.execute(insert(task_completion_records).values(id=generation,
                task_id=tid, outcome="pass", branch=branch, commits=json.dumps([source]),
                pr_url=url, notes=json.dumps({"backmerge": meta}), completed_at=now)
                .on_conflict_do_update(index_elements=["id"], set_={"branch": branch,
                    "commits": json.dumps([source]), "pr_url": url,
                    "notes": json.dumps({"backmerge": meta})}))
            completion_id = generation
            await conn.execute(insert(task_metadata).values(task_id=tid,
                key=DEVELOPMENT_COMPLETION_ID_KEY, value=json.dumps(completion_id)))
            await conn.execute(insert(task_metadata).values(task_id=tid, key="backmerge",
                value=json.dumps(meta, sort_keys=True)).on_conflict_do_update(
                    index_elements=["task_id", "key"], set_={"value": json.dumps(meta, sort_keys=True)}))
        return {"task_id": tid, "pr_url": url, **meta}

    async def eligible(self, batch, member, snapshot):
        """Recheck daemon identity and originating Git head, including at publication."""
        async with self.db._engine.connect() as conn:
            row = (await conn.execute(select(tasks, task_metadata.c.value.label("backmerge"))
                .join(task_metadata, task_metadata.c.task_id == tasks.c.id)
                .where(tasks.c.id == member.task_id, task_metadata.c.key == "backmerge"))).mappings().first()
        if (not row or row["project_id"] != batch.project_id or row["repo_id"] != batch.repository_id
                or row["task_type"] != "backmerge" or row["profile_id"] is not None
                or row["created_by_kind"] != "backmerge" or row["created_by_id"] != TRAIN_HOLDER
                or row["status"] != "COMPLETED"
                or snapshot is None or snapshot.error):
            return False
        meta = json.loads(row["backmerge"])
        if (meta["kind"] != "source" or meta["target_ref"] != batch.target_ref
                or meta["source_sha"] != member.source_sha
                or meta["base_sha"] != member.base_sha):
            return False
        origin = snapshot.for_target(meta["origin_ref"]).target_oid
        if not origin:
            return False
        observed = snapshot.observation
        result = await observed.git.arun_git_result(
            ["merge-base", "--is-ancestor", member.source_sha, origin], cwd=observed.store,
        )
        return result.returncode == 0


class DatabaseBatches:
    """The open batch of a target, frozen from exact undelivered completions."""

    STACK_CACHE_LIMIT = 256

    def __init__(self, db, *, limit: int = MEMBER_LIMIT, clock: Callable[[], float] = time.time,
                 pr_gate: Callable | None = None, cleanup=None):
        self.db, self.limit, self.clock = db, limit, clock
        self._stack_scan = {}
        self.pr_gate = pr_gate
        self.repaired_source_gate = getattr(pr_gate, "admit_repaired_source", None)
        self.cleanup = cleanup
        self._refresh_snapshots = {}
        #: epic task id -> ((epic head, default head), refresh detail)
        self._conflict_refreshes: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}

    async def _refresh_conflicting_epics(self, target, snapshot, blockers) -> None:
        """Start one epic refresh per conflicting (epic head, default head) pair.

        GitHub runs no pull_request checks for a PR that conflicts with its
        base, so the root PR of a stale epic would wait for them forever. The
        refresh is the attested one ``refresh-epic --apply`` starts: a frozen
        batch on the epic that this train's own visits test and publish. The
        refreshed head then needs its own exact-head PR checks.
        """
        from src.integration.stacked_branches import EpicRefresh

        for index, blocker in enumerate(blockers):
            if blocker.get("code") != "pr_conflicting" or not blocker.get("epic"):
                continue
            task_id = blocker["task_id"]
            pair = (blocker["source_sha"], blocker["default_sha"])
            cached = self._conflict_refreshes.get(task_id)
            previous = cached[1] if cached is not None and cached[0] == pair else None
            refresh = previous
            try:
                if refresh is not None and refresh["outcome"] in {"started", "running"}:
                    async with self.db._engine.connect() as conn:
                        open_id = await conn.scalar(_open_batch_rows(
                            target.project_id, target.repository_id,
                        ).with_only_columns(integration_batches.c.id).where(
                            integration_batches.c.id == refresh["batch_id"]))
                    # A running batch can belong to an older head pair. Once
                    # it settles, start() must reconsider this pair. Its durable
                    # batch identity still prevents repeating a settled pair.
                    refresh = {**refresh, "outcome": "running"} if open_id else None
                if (refresh is not None and refresh["outcome"] == "unavailable"
                        and self.clock() >= refresh["retry_at"]):
                    refresh = None
                if refresh is None:
                    result = await EpicRefresh(self.db, clock=self.clock).start(
                        task_id, snapshot=snapshot)
                    refresh = {key: result[key] for key in ("outcome", "batch_id") if key in result}
                    if result["outcome"] == "started":
                        await self.db.log_event(
                            "integration.epic_refresh", project_id=target.project_id,
                            task_id=task_id, payload=json.dumps({
                                **result, "trigger": "pr_conflicting", "pr_url": blocker["pr_url"]}))
            except Exception as exc:
                # A local refresh failure withholds only this epic, allowing
                # unrelated roots through their own PR gates. Retry with capped
                # backoff; exception text can contain credentials or DB inputs.
                logger.warning("Automatic epic refresh unavailable for %s", task_id,
                               exc_info=True)
                delay = min((previous or {}).get("retry_seconds", 30) * 2, 600)
                refresh = {"outcome": "unavailable", "reason": type(exc).__name__,
                           "retry_at": self.clock() + delay, "retry_seconds": delay}
            # An ordinary child batch still owns the epic: try again next visit.
            if refresh["outcome"] == "pending":
                self._conflict_refreshes.pop(task_id, None)
            else:
                self._conflict_refreshes[task_id] = (pair, refresh)
            blockers[index] = _conflict_action(blocker, snapshot, epic=True, refresh=refresh)

    async def open_batch(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, service: BatchService, *,
        seal_now: bool = False,
    ) -> BatchSelection:
        with selection_stage("reconcile_aborted"):
            await service.store.reconcile_aborted(target=target)
        # One fetched observation per serialized visit. Eligibility is called
        # again inside publication locks and must only read these local facts.
        self._refresh_snapshots[target.key] = snapshot
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db, clock=self.clock)
        admission_blockers = []
        delivered_ids = set()
        candidates = (await self._candidate_ids(target, snapshot, blockers=admission_blockers,
                                                delivered_ids=delivered_ids)
                      if not snapshot.error and snapshot.target_oid else [])
        async with self.db._engine.connect() as conn:
            stacked_ids = (await conn.execute(select(task_branch_origins.c.task_id).where(
                    task_branch_origins.c.task_id.in_(candidates),
                    task_branch_origins.c.repository_id == target.repository_id,
                    task_branch_origins.c.retired_at.is_(None),
                    task_branch_origins.c.stack_snapshot.is_not(None)))).scalars().all()
        for task_id in stacked_ids:
            with selection_stage("stack_refresh", items=1):
                outcome = await _finish_on_cancel(
                    stacks.refresh(task_id, service.gitops, snapshot=snapshot))
            if outcome in {"refreshed", "changed", "repair_filed"}:
                return BatchSelection(blockers=({"code": "stack_" + outcome, "task_id": task_id,
                    "ref": task_id, "detail": "Prerequisite stack changed; take a fresh Git view"},))
        # One live batch owns a target: a replaced stale batch must not block the freeze.
        with selection_stage("supersede_batches"):
            blockers = list(await self.supersede_refreshed(target, service))
        if blockers:
            # An unauditable refresh keeps its old batch and target ownership.
            # Do not try to freeze a replacement without a release instruction.
            return BatchSelection(blockers=tuple(blockers))
        blockers.extend(admission_blockers)
        current = await self.current(target)
        frozen_members = await service.store.members(current.id) if current is not None else ()
        pending = None
        if not snapshot.error and snapshot.target_oid:
            pending = await self.pending(target, snapshot, blockers=blockers,
                                         gate_pr=current is None,
                                         include_ids={member.task_id for member in frozen_members},
                                         candidate_ids=candidates, delivered_ids=delivered_ids)
            await _finish_on_cancel(self._refresh_conflicting_epics(target, snapshot, blockers))
        if current is not None:
            if target.kind == "root":
                await self._clear_admissions(target)
            members = frozen_members
            if any(blocker.get("task_id") == member.task_id
                   and blocker.get("source_sha") == member.source_sha
                   and blocker.get("source_base_sha") == member.base_sha
                   for blocker in blockers for member in members):
                return BatchSelection(blockers=tuple(
                    {**blocker, "batch_id": current.id} for blocker in blockers
                ))
            # Retired members disappear from the pending frontier but remain
            # in a frozen batch. Explain why its publication is now refused,
            # and release the target rather than hold it until an operator
            # aborts a batch that can never publish.
            async with self.db._engine.connect() as conn:
                undeliverable = await undeliverable_repairs_on(
                    self.db, conn, [member.task_id for member in members],
                    repository_id=target.repository_id,
                )
            reported = {(blocker["code"], blocker.get("task_id")) for blocker in blockers}
            blockers.extend(blocker for task_id, blocker in sorted(undeliverable.items())
                            if (blocker["code"], task_id) not in reported)
            if undeliverable:
                # An explicitly paused batch is never released here: it falls
                # through to the ordinary held visit with its blocker named.
                released = await self._release_undeliverable_batch(
                    service, current, undeliverable,
                )
                if released:
                    # One report per cause: the release supersedes the naming
                    # that admission already gave the same repair.
                    named = {(blocker["code"], blocker.get("task_id"))
                             for blocker in released}
                    blockers = [blocker for blocker in blockers
                                if (blocker["code"], blocker.get("task_id")) not in named]
                    blockers.extend(released)
                    return BatchSelection(blockers=tuple(
                        {**blocker, "batch_id": current.id} for blocker in blockers
                    ))
            # Old frozen inputs also stay out of duplicate epic publication.
            if target.kind == "epic" and not (current.epic_sync or current.epic_refresh):
                delivered = await self.delivered(target, snapshot, [m.task_id for m in members])
                if delivered:
                    blockers.append({
                        "code": "batch_inputs_delivered_to_project", "ref": current.id,
                        "detail": "batch contains work already delivered to the project root; "
                                  "abort the batch before retiring its origins",
                        "task_ids": sorted(delivered),
                    })
                    return BatchSelection(blockers=tuple(blockers))
            return BatchSelection(current, members, tuple(blockers), existing=True)
        if pending is None:
            if target.kind == "root" and not snapshot.error and snapshot.target_oid and not blockers:
                await self._clear_admissions(target)
            return BatchSelection(blockers=tuple(blockers))
        members, requests, dependencies = pending
        now = self.clock()
        if target.kind == "root":
            policy = await self._train_policy(target)
            admissions = await self._admission_times(target, members, requests, now)
            first, latest = min(admissions), max(admissions)
            seal_at = min(latest + policy.cadence_seconds, first + policy.settling_cap_seconds)
            if not seal_now and now < seal_at:
                return BatchSelection(blockers=tuple(blockers), detail={
                    "reason": "settling", "first_admission_at": first,
                    "latest_admission_at": latest, "seal_at": seal_at,
                    "cadence_seconds": policy.cadence_seconds,
                    "settling_cap_seconds": policy.settling_cap_seconds,
                    "task_ids": [member.task_id for member in members],
                })
        batch = Batch(id=await self.next_batch_id(target, members), project_id=target.project_id,
                      repository_id=target.repository_id, target_ref=target.target_ref,
                      created_at=now)
        with selection_stage("freeze", items=len(members)):
            frozen = await service.freeze(batch, members, requests=requests, snapshot=snapshot,
                                          dependencies=dependencies)
        if target.kind == "root":
            await self._clear_admissions(target)
        return BatchSelection(frozen, await service.store.members(frozen.id), tuple(blockers))

    async def next_batch_id(self, target, members):
        """An ejected singleton may return with the same exact frozen inputs."""
        identity = batch_id(target, members)
        async with self.db._engine.connect() as conn:
            while True:
                row = (await conn.execute(select(integration_batches.c.intent,
                    ejection_instruction(integration_batches.c.id).label("ejected")).where(
                        integration_batches.c.id == identity,
                    ))).first()
                if row is None or row.intent != "aborted" or not row.ejected:
                    return identity
                identity = "train-" + hashlib.sha256((identity + ":readmit").encode()).hexdigest()[:32]

    async def current(self, target: TrainTarget) -> Batch | None:
        return next((batch for batch, refreshed in await self._open_batches(target)
                     if refreshed is None), None)

    async def _release_undeliverable_batch(
        self, service: BatchService, batch: Batch, undeliverable: dict[str, dict],
    ) -> list[dict[str, Any]]:
        """Release a batch a superseded or failed repair can never publish.

        Publication is already refused for such a member, so an open batch only
        holds its target against every other source.  On the box where this was
        found, a superseded source-CI repair kept a development lane for fifty
        minutes while the corrected source it was filed against waited behind
        it, and only an operator abort freed the target.  Aborting returns the
        frozen inputs to admission through the same instruction a refreshed
        stack uses, so the next visit selects without the dead member.  A task
        identity that cannot be resolved to exactly one project is never
        released automatically and names the operator command instead.
        """
        refusals = []
        for task_id, blocker in sorted(undeliverable.items()):
            try:
                released = await service.store.supersede(batch, task_id, reason=(
                    f"source-CI repair {task_id} can no longer be delivered: "
                    f"{blocker['detail']}"))
            except SupersedeMemberUnavailable as exc:
                refusals.append({"code": exc.code, "ref": exc.task_id,
                                 "task_id": exc.task_id, "batch_id": batch.id,
                                 "detail": f"{exc}; restore an unambiguous task identity in "
                                           f"the batch project or have an operator use aq "
                                           f"integration abort-batch {batch.id} --reason "
                                           f"<reason> --apply; an ordinary abort keeps the "
                                           f"batch's frozen inputs withheld"})
                continue
            if released:
                refusals.append({**blocker, "released": True,
                                 "detail": f"{blocker['detail']}; the batch released this "
                                           f"target and a new one replaces it"})
                break
        return refusals

    async def supersede_refreshed(
        self, target: TrainTarget, service: BatchService,
    ) -> tuple[dict[str, Any], ...]:
        """Supersede auditable refreshes; block invalid member identities without releasing work."""
        blockers = []
        for batch, refreshed in await self._open_batches(target):
            if refreshed is not None:
                try:
                    await service.store.supersede(batch, refreshed, reason=(
                        f"stacked source of {refreshed} was refreshed; a new batch replaces it"))
                except SupersedeMemberUnavailable as exc:
                    blockers.append({"code": exc.code, "ref": exc.task_id,
                        "task_id": exc.task_id, "batch_id": batch.id,
                        "detail": f"{exc}; restore an unambiguous task identity in the batch "
                                  "project or have an "
                                  f"operator use aq integration abort-batch {batch.id} "
                                  "--reason <reason> --apply; an ordinary abort keeps the "
                                  "batch's frozen inputs withheld"})
        return tuple(blockers)

    async def _open_batches(self, target: TrainTarget) -> list[tuple[Batch, str | None]]:
        """Open batches oldest first, each with the member whose stack was refreshed."""
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db)
        found = []
        async with self.db._engine.connect() as conn:
            query = _open_batch_rows(target.project_id, target.repository_id).where(
                integration_batches.c.target_ref == target.target_ref)
            if target.kind == "promotion":
                query = query.where(integration_batches.c.trigger == "promotion")
            rows = (await conn.execute(
                query
                .order_by(case((integration_batches.c.policy_snapshot["promotion_intent"]
                                ["kind"].as_string() == "backmerge", 1), else_=0),
                          integration_batches.c.created_at, integration_batches.c.id)
            )).mappings().all()
            for row in rows:
                # A refreshed stack needs a new exact candidate; it must not be
                # trapped behind its former source. Explicit operator pause still holds.
                refreshed = None
                if row["intent"] == "open":
                    for tid, source in (await conn.execute(select(
                        integration_batch_members.c.task_id, integration_batch_members.c.source_sha,
                    ).where(integration_batch_members.c.batch_id == row["id"]))).all():
                        origin = await stacks._origin(tid, conn)
                        stack = origin and origin["stack_snapshot"]
                        if (stack and not stack.get("hold") and stack.get("refreshed_head")
                                and stack["refreshed_head"] != source):
                            refreshed = tid
                            break
                found.append((Batch.from_row(row), refreshed))
        return found

    @staticmethod
    def _admission_key(target: TrainTarget) -> str:
        return "integration_train_admission:" + hashlib.sha256(repr(target.key).encode()).hexdigest()

    async def _admission_times(
        self, target: TrainTarget, members: tuple[BatchMember, ...],
        requests: dict[str, DeliveryRequest], now: float,
    ) -> tuple[float, ...]:
        """Durable timing hints for currently PR-admitted exact completions.

        These hints never authorize membership or prove delivery. Every visit
        derives eligibility again before consulting them, so a restart keeps
        the timing bound without introducing another code-state authority.
        """
        key = self._admission_key(target)
        times = []
        async with self.db.immediate() as conn:
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.key == key,
                task_metadata.c.task_id.not_in([member.task_id for member in members]),
            ))
            for member in members:
                request = requests[member.task_id]
                identity = [request.completion_id or request.legacy_generation,
                            member.source_sha, member.source_base_sha]
                value = json.dumps({"identity": identity, "admitted_at": now})
                await conn.execute(insert(task_metadata).values(
                    task_id=member.task_id, key=key, value=value,
                ).on_conflict_do_nothing(index_elements=["task_id", "key"]))
                previous = await conn.scalar(select(task_metadata.c.value).where(
                    task_metadata.c.task_id == member.task_id, task_metadata.c.key == key,
                ).with_for_update())
                try:
                    recorded = json.loads(previous)
                    admitted = float(recorded["admitted_at"])
                    if (recorded["identity"] != identity or not math.isfinite(admitted)
                            or admitted > now):
                        raise ValueError("admission identity or clock changed")
                except (TypeError, ValueError, KeyError):
                    admitted = now
                    await conn.execute(update(task_metadata).where(
                        task_metadata.c.task_id == member.task_id, task_metadata.c.key == key,
                    ).values(value=value))
                times.append(admitted)
        return tuple(times)

    async def _clear_admissions(self, target: TrainTarget) -> None:
        async with self.db.immediate() as conn:
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.key == self._admission_key(target),
            ))

    async def _train_policy(self, target: TrainTarget) -> IntegrationTrainPolicy:
        async with self.db._engine.connect() as conn:
            policy = await conn.scalar(select(projects.c.hierarchical_integration_policy).where(
                projects.c.id == target.project_id,
            ))
        raw = (policy or {}).get("train")
        return IntegrationTrainPolicy.model_validate({} if raw is None else raw)

    async def _candidate_ids(self, target: TrainTarget, snapshot: GitTruthSnapshot, *,
                             include_ids: set[str] | None = None, blockers=None,
                             delivered_ids: set[str] | None = None):
        """Only undelivered completions routed to this target's member window."""
        if target.kind == "promotion":
            # A promotion freezes its request, never a frontier of completions.
            return []
        selection_count("candidate_scans")
        with selection_stage("pending_ids"):
            async with self.db._engine.connect() as conn:
                ids = await _pending_tasks(conn, target.project_id, target.repository_id, limit=None)
        selection_count("repository_ids", len(ids))
        with selection_stage("route_ids", items=len(ids)):
            async with self.db._engine.connect() as conn:
                routed = await delivery_targets(conn, ids, reduced=True)
            ids = [task_id for task_id in ids if task_id in routed and
                   (routed[task_id].repository_id, routed[task_id].target_ref) ==
                   (target.repository_id, target.target_ref)]
        selection_count("routed_ids", len(ids))
        async with self.db._engine.connect() as conn:
            superseded = await superseded_source_repairs_on(
                self.db, conn, ids, repository_id=target.repository_id,
            )
        if blockers is not None:
            blockers.extend(superseded.values())
        ids = [task_id for task_id in ids if task_id not in superseded]
        with selection_stage("root_delivery", items=len(ids)):
            delivered_to_project = await self.delivered(target, snapshot, ids)
        if delivered_ids is not None:
            delivered_ids.update(delivered_to_project)
        ids = [task_id for task_id in ids if task_id not in delivered_to_project]
        async with self.db._engine.connect() as conn:
            if len(ids) > self.limit:
                frozen_ids = [task_id for task_id in ids if task_id in (include_ids or ())]
                ids = list((await conn.execute(select(tasks.c.id).where(tasks.c.id.in_(ids))
                           .order_by(tasks.c.updated_at.desc(), tasks.c.id)
                           .limit(self.limit))).scalars().all())
                ids = list(dict.fromkeys([*ids, *frozen_ids]))
        return ids

    async def pending(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, *,
        blockers: list[dict[str, Any]] | None = None,
        gate_pr: bool = True,
        include_ids: set[str] | None = None,
        candidate_ids: list[str] | None = None,
        delivered_ids: set[str] | None = None,
    ):
        """Exact pending inputs; report unknown delivery that prevents batching."""
        if target.kind == "promotion":
            return None
        delivered_to_project = set(delivered_ids or ())
        if candidate_ids is None:
            ids = await self._candidate_ids(target, snapshot, include_ids=include_ids, blockers=blockers,
                                            delivered_ids=delivered_to_project)
        else:
            selection_count("candidate_window_reuses")
            ids = candidate_ids
            if include_ids:
                ids = list(dict.fromkeys([*ids, *sorted(include_ids)]))
        async with self.db._engine.connect() as conn:
            if not ids:
                return None
            # Admission withholds a superseded or terminal repair however the
            # ids arrived: a frozen member that lost its purpose is not
            # pending work again, and re-admitting it would publish work the
            # reopen already discarded.
            undeliverable = await undeliverable_repairs_on(
                self.db, conn, ids, repository_id=target.repository_id,
            )
            if undeliverable:
                if blockers is not None:
                    blockers.extend(undeliverable.values())
                ids = [task_id for task_id in ids if task_id not in undeliverable]
                if not ids:
                    return None
            epics = await _epic_branches_on(conn, ids)
            withheld = set((await conn.execute(
                select(integration_batch_members.c.task_id, integration_batch_members.c.source_sha)
                .select_from(integration_batch_members.join(
                    integration_batches,
                    integration_batches.c.id == integration_batch_members.c.batch_id))
                .where(integration_batches.c.project_id == target.project_id,
                       integration_batches.c.repository_id == target.repository_id,
                       integration_batches.c.target_ref == target.target_ref,
                       integration_batches.c.intent == "aborted",
                       ~ejection_instruction(integration_batches.c.id),
                       integration_batch_members.c.task_id.in_(ids))
            )).all())
            repairs = set((await conn.execute(select(task_metadata.c.task_id).where(
                task_metadata.c.task_id.in_(ids), task_metadata.c.key == "stack_repair_for",
            ))).scalars())
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
                .join(tasks, tasks.c.id == task_dependencies.c.task_id)
                .where(tasks.c.project_id == target.project_id,
                       task_dependencies.c.dep_type == "blocks")
            )).all():
                edges.setdefault(task_id, set()).add(needs)
            repair_bindings = await _source_ci_prerequisites_on(
                conn, ids, bases, repository_id=target.repository_id,
            )
        # A declared prerequisite can be reached through another prerequisite.
        # Keep that ordering when the intervening task is already delivered.
        direct_edges = edges
        edges = {}
        for task_id in ids:
            needs, remaining = set(), list(direct_edges.get(task_id, ()))
            while remaining:
                need = remaining.pop()
                if need not in needs:
                    needs.add(need)
                    remaining.extend(direct_edges.get(need, ()))
            edges[task_id] = needs
        # A daemon-written source-CI repair binding is a declared stack too, so
        # the repair lands with its source in one batch instead of waiting for a
        # source that cannot go green without the repair. An edge contradicting
        # the declared order is left alone: the guard then names the real
        # blocker instead of a cycle freezing nothing.
        for repair_id, sources in repair_bindings.items():
            for source_id in sorted(sources):
                if not _reaches(edges, source_id, repair_id):
                    edges.setdefault(repair_id, set()).add(source_id)
                    direct_edges.setdefault(repair_id, set()).add(source_id)
        requests = await load_delivery_requests(
            self.db, ids, repository_id=target.repository_id, target_ref=target.target_ref,
            reduced=True,
        )
        members: dict[str, BatchMember] = {}
        refused_sources = {}
        paired_sources = {}
        delivered: set[str] = set()
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db)
        for task_id in ids:
            request, base = requests.get(task_id), bases.get(task_id)
            if task_id in repairs:
                continue
            if request is None or not is_valid_git_oid(base or ""):
                continue
            with selection_stage("target_delivery", items=1):
                evidence = await snapshot.is_delivered(request, source_base=base)
            if evidence.satisfied:
                delivered.add(task_id)
                continue
            # Stack freshness gates new batching; it cannot undo proven delivery.
            with selection_stage("stack_identity", items=1):
                stack_current = await stacks.current(task_id)
            if not stack_current:
                if blockers is not None:
                    async with self.db._engine.connect() as conn:
                        origin = await stacks._origin(task_id, conn)
                    hold = (origin and origin["stack_snapshot"] or {}).get("hold", "changed")
                    blockers.append({"code": "stack_" + hold, "task_id": task_id,
                        "ref": task_id, "detail": "Prerequisite stack is withheld: " + hold})
                continue
            # Epic readiness gates new batching; it cannot undo proven delivery.
            if task_id in epics:
                with selection_stage("epic_readiness", items=1):
                    async with self.db._engine.connect() as conn:
                        current = await self._epic_current_on(conn, task_id, request.completion_id,
                                                             snapshot=snapshot,
                                                             source=evidence.source_oid)
                if not current:
                    if blockers is not None:
                        blockers.append({"code": "epic_completion_pending", "ref": task_id,
                            "task_id": task_id, "detail": "epic needs a current collected completion"})
                    continue
            if task_id in epics and evidence.source_oid != snapshot.for_target(
                    _branch(epics[task_id])).target_oid:
                if blockers is not None:
                    blockers.append({"code": "epic_source_changed", "ref": task_id,
                        "task_id": task_id, "detail": "epic head changed after completion"})
                continue
            if evidence.state is DeliveryState.UNKNOWN and blockers is not None:
                detail = f"task {task_id} delivery is unknown ({evidence.reason})"
                if evidence.error_detail:
                    detail += f": {evidence.error_detail}"
                blockers.append({
                    "code": evidence.reason, "ref": task_id, "task_id": task_id,
                    "detail": detail,
                    "repository_id": target.repository_id, "target_ref": target.target_ref,
                })
            source = evidence.source_oid
            if (evidence.state is not DeliveryState.PENDING
                    or evidence.reason != "source_not_delivered"
                    or not is_valid_git_oid(source or "") or (task_id, source) in withheld):
                continue
            with selection_stage("stack_source", items=1):
                stack_valid = (await stacks.current(task_id, source_sha=source)
                               and await stacks.source_contains_stack(task_id, source, snapshot))
            if not stack_valid:
                continue
            member = BatchMember(task_id, source, base)
            if gate_pr and target.kind == "root":
                with selection_stage("pr_admission", items=1):
                    refusal = (await self.pr_gate(target, member) if self.pr_gate else {
                        "code": "unknown", "ref": task_id, "task_id": task_id,
                        "detail": "root PR admission observer is unavailable",
                    })
                if refusal:
                    if refusal["code"] == "pr_checks_red":
                        refused_sources[task_id] = (member, refusal)
                    if blockers is not None:
                        blockers.append(_conflict_action(refusal, snapshot, epic=task_id in epics))
                    continue
            members[task_id] = member
        # A genuinely red source can travel only with its exact authorized,
        # separately admitted repair. Review authorization is observed again;
        # final candidate checks still validate both trees together.
        admit_repaired = self.repaired_source_gate
        if admit_repaired is not None:
            for repair_id, sources in repair_bindings.items():
                if repair_id not in members:
                    continue
                for source_id in sorted(sources):
                    if source_id not in refused_sources or source_id not in edges.get(repair_id, set()):
                        continue
                    member, refusal = refused_sources[source_id]
                    if member.source_sha != bases.get(repair_id):
                        continue
                    if await admit_repaired(target, member) is None:
                        members[source_id] = member
                        paired_sources.setdefault(source_id, set()).add(repair_id)
                        if blockers is not None and refusal in blockers:
                            blockers.remove(refusal)
        unsafe = await self._stacked_bases(
            target, snapshot, members, edges, delivered | delivered_to_project, blockers,
            frozen_ids=include_ids,
        )
        for task_id in unsafe:
            members.pop(task_id)
        # A member never lands ahead of undelivered work it depends on that
        # this batch does not carry; it waits for a later batch instead.
        # Include prerequisites outside the member window: reopened, aborted and
        # capped-out sources must not disappear from the admission rule.
        required = set().union(*edges.values()) if edges else set()
        absent = required - members.keys() - delivered
        outside = await load_delivery_requests(self.db, absent,
            repository_id=target.repository_id, target_ref=target.target_ref, reduced=True)
        async with self.db._engine.connect() as conn:
            outside_bases = dict((await conn.execute(select(
                task_branch_origins.c.task_id, task_branch_origins.c.base_sha,
            ).where(task_branch_origins.c.task_id.in_(absent),
                    task_branch_origins.c.retired_at.is_(None)))).all())
        for task_id, request in outside.items():
            with selection_stage("prerequisite_delivery", items=1):
                if request.task_status == "COMPLETED" and (await snapshot.is_delivered(
                        request, source_base=outside_bases.get(task_id))).satisfied:
                    delivered.add(task_id)
        # Proven delivery settles an intervening prerequisite. Reopening its
        # own ancestor cannot revoke that delivery or block descendants whose
        # immediate prerequisite already landed on this target.
        admission_edges = {}
        for task_id in members:
            needs, remaining = set(), list(direct_edges.get(task_id, ()))
            while remaining:
                need = remaining.pop()
                if need not in needs and need not in delivered:
                    needs.add(need)
                    remaining.extend(direct_edges.get(need, ()))
            admission_edges[task_id] = needs
        blocked = (set(ids) | required) - members.keys() - delivered
        changed = True
        while changed:
            changed = False
            for task_id in list(members):
                if (admission_edges.get(task_id, set()) & blocked
                        or (task_id in paired_sources
                            and not paired_sources[task_id] & members.keys())):
                    if task_id in paired_sources and blockers is not None:
                        refusal = refused_sources[task_id][1]
                        if refusal not in blockers:
                            blockers.append(refusal)
                    blocked.add(members.pop(task_id).task_id)
                    changed = True
        if not members:
            return None
        dependencies = {task_id: admission_edges.get(task_id, set()) & members.keys()
                        for task_id in members}
        return tuple(members.values()), {key: requests[key] for key in members}, dependencies

    async def _stacked_bases(
        self, target, snapshot, members, edges, delivered, blockers, *, frozen_ids=None,
    ):
        """Do not import another task's ancestry without its content or dependency.

        A merge of ``base..source`` retains source ancestry even when the target
        never received the base's tree changes. A later target sync can therefore
        delete those changes from their owning branch. Only an explicit blocks
        prerequisite -- or the daemon's own source-CI repair binding, which is
        one -- authorizes a base from another task's unpublished history.
        Use the visit's pinned OIDs, including branches still being worked on.
        """
        observed = snapshot.observation
        bases = {}
        for base in sorted({member.base_sha for member in members.values()}):
            ancestor = await observed.git.ais_ancestor(
                observed.store, base, snapshot.target_oid, strict=True,
            )
            if ancestor is not True:
                bases[base] = ancestor
        if not bases:
            return set()
        async with self.db._engine.connect() as conn:
            origins = (await conn.execute(select(task_branch_origins).where(
                task_branch_origins.c.repository_id == target.repository_id,
                task_branch_origins.c.retired_at.is_(None),
                task_branch_origins.c.materialized.is_(True),
            ).order_by(task_branch_origins.c.task_id))).mappings().all()
        unsafe = set()

        def block(member, code, detail, **facts):
            unsafe.add(member.task_id)
            if blockers is not None:
                blockers.append({"code": code, "ref": member.task_id,
                    "task_id": member.task_id, "detail": detail,
                    "source_base_sha": member.base_sha, "source_sha": member.source_sha,
                    "repository_id": target.repository_id, "target_ref": target.target_ref,
                    **facts})

        for member in members.values():
            if member.base_sha in bases and bases[member.base_sha] is None:
                block(member, "stack_ancestry_unknown", "source base ancestry is unavailable")
        heads = _origin_heads(observed, origins)
        # One containment query per off-target base answers for every origin at
        # once, so an unrelated live branch costs this visit no git call and an
        # unreadable one cannot withhold a member that never needed it.
        published = {}
        for base in sorted(bases):
            if bases[base] is not None:
                published[base] = await self._base_publishers(
                    observed, origins, heads, base, delivered,
                )
        for member in members.values():
            if member.base_sha not in bases or bases[member.base_sha] is None:
                continue
            scan = published.get(member.base_sha)
            if scan is None:
                block(member, "stack_ancestry_unknown",
                      "foreign task history is unavailable for this source base")
                continue
            holding, publishers = scan[0] - {member.task_id}, scan[1] - {member.task_id}
            if not publishers:
                continue
            # A declared prerequisite holding the base publishes it, so a
            # sibling which merely merged that branch is not the task to name.
            declared = sorted(holding & edges.get(member.task_id, set()))
            if declared:
                for prerequisite in declared:
                    available = prerequisite in members
                    if member.task_id in (frozen_ids or ()):
                        available = available and prerequisite in frozen_ids
                    if not available and prerequisite not in delivered:
                        block(member, "stack_prerequisite_pending",
                              f"stack prerequisite {prerequisite} is neither delivered "
                              "nor available in this batch",
                              prerequisite_task_id=prerequisite,
                              prerequisite_head_sha=heads.get(prerequisite))
                continue
            prerequisite = min(publishers)
            block(member, "undeclared_stack_base",
                  f"source base includes unpublished work from task {prerequisite}; "
                  "declare a blocks prerequisite or rebuild from the delivery target",
                  prerequisite_task_id=prerequisite,
                  prerequisite_head_sha=heads.get(prerequisite),
                  prerequisite_task_ids=sorted(publishers))
        return unsafe

    async def _base_publishers(self, observed, origins, heads, base, delivered):
        """Live task branches that hold this off-target base, and those that publish it.

        ``holding`` is every live branch whose fetched head contains the base;
        ``publishers`` drops the ones whose own fork point already had it,
        because such a base is not their unpublished work. Retired, unmaterialized
        and delivered origins never take part: their content already reached a
        target, so they can neither hold a base this visit must refuse nor cost
        it a probe. ``None`` means the ancestry this base needs was unreadable,
        and withholds only the members that share that base.
        """
        live = tuple(origins)
        key = (base, observed.target_oid or "", _stack_snapshot_digest(observed, live, delivered))
        cached = self._stack_scan.get(key)
        if cached is not None:
            return cached
        result = await observed.git.arun_git_result([
            "--no-replace-objects", "for-each-ref", "--format=%(refname)",
            "--contains", base, "refs/remotes/origin/",
        ], cwd=observed.store, env={"LC_ALL": "C"})
        if result.returncode:
            return None
        containing = set(result.stdout.split())
        holding = {
            origin["task_id"] for origin in live
            if heads.get(origin["task_id"]) and origin["task_id"] not in delivered
            and "refs/remotes/origin/" + origin["branch_name"].removeprefix(
                "refs/heads/") in containing
        }
        publishers = set()
        for origin in live:
            if origin["task_id"] not in holding:
                continue
            publishes = await observed.git.ais_ancestor(
                observed.store, base, origin["base_sha"], strict=True,
            )
            if publishes is None:
                return None
            if publishes:
                continue
            publishers.add(origin["task_id"])
        answer = (frozenset(holding), frozenset(publishers))
        if len(self._stack_scan) >= self.STACK_CACHE_LIMIT:
            self._stack_scan.clear()
        self._stack_scan[key] = answer
        return answer

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
        if batch.epic_refresh:
            from src.integration.stacked_branches import EpicRefresh

            if len(members) != 1:
                return False
            async with self.db._engine.connect() as conn:
                row = await EpicRefresh(self.db).identity(members[0].task_id, conn=conn)
                active = await conn.scalar(select(projects.c.status).where(
                    projects.c.id == batch.project_id))
            if not (row and active == "ACTIVE" and row["project_id"] == batch.project_id
                    and row["repo_id"] == batch.repository_id
                    and _branch(row["branch_name"]) == batch.target_ref):
                return False
            key = (batch.project_id, batch.repository_id, batch.target_ref)
            observed = self._refresh_snapshots.get(key)
            if observed is not None:
                observed = observed.for_target(_branch(row["default_branch"]))
            if observed is None or observed.error or not observed.target_oid:
                return False
            result = await observed.observation.git.arun_git_result(
                ["merge-base", "--is-ancestor", members[0].source_sha, observed.target_oid],
                cwd=observed.observation.store)
            return result.returncode == 0
        ids = [member.task_id for member in members]
        async with self.db._engine.connect() as conn:
            if await superseded_source_repairs_on(
                self.db, conn, ids, repository_id=batch.repository_id,
            ):
                return False
            mode = (await conn.execute(
                select(projects.c.hierarchical_integration_mode, projects.c.status,
                       projects.c.promotion_flow, projects.c.default_branch_cutover)
                .where(projects.c.id == batch.project_id)
            )).first()
            routed = await delivery_targets(conn, ids, reduced=True)
            flow = () if mode is None else mode.promotion_flow or ()
            promotion_refs = {
                ref for name in flow_targets(flow) for ref in (name, _branch(name))
            }
            from src.integration.promotion_routing import cutover_origin_clause

            cutover = None if mode is None else mode.default_branch_cutover
            live = set((await conn.execute(
                select(task_branch_origins.c.task_id)
                .select_from(task_branch_origins.join(
                    tasks, tasks.c.id == task_branch_origins.c.task_id))
                .where(
                    task_branch_origins.c.task_id.in_(ids),
                    task_branch_origins.c.repository_id == batch.repository_id,
                    task_branch_origins.c.retired_at.is_(None),
                    or_(tasks.c.task_type.is_(None), tasks.c.task_type != "promotion"),
                    or_(task_branch_origins.c.parent_ref.is_(None),
                        task_branch_origins.c.parent_ref.not_in(promotion_refs),
                        cutover_origin_clause(cutover, batch.repository_id)),
                )
            )).scalars().all())
        if mode is None or mode[0] not in TRAIN_MODES or mode[1] != "ACTIVE":
            return False
        if batch.epic_sync and await self.closed_epic_graph(TrainTarget(
                batch.project_id, batch.repository_id, batch.target_ref, "epic")) is None:
            return False
        if batch.target_ref in promotion_refs:
            return False
        requests = await load_delivery_requests(
            self.db, ids, repository_id=batch.repository_id, target_ref=batch.target_ref,
            reduced=True,
        )
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db)
        for member in members:
            if not await stacks.current(member.task_id, source_sha=member.source_sha):
                return False
        source_types = {}
        async with self.db._engine.connect() as conn:
            source_types = dict((await conn.execute(select(tasks.c.id, tasks.c.task_type)
                                                  .where(tasks.c.id.in_(ids)))).all())
        for member in members:
            if source_types.get(member.task_id) == "backmerge":
                observed = self._refresh_snapshots.get(
                    (batch.project_id, batch.repository_id, batch.target_ref))
                if not await BackmergeAdmission(self.db, None).eligible(batch, member, observed):
                    return False
        for task_id in ids:
            request, target = requests.get(task_id), routed.get(task_id)
            if ((not batch.epic_sync and task_id not in live)
                    or request is None or request.task_status != "COMPLETED" or
                    target is None or
                    (target.repository_id, target.target_ref) !=
                    (batch.repository_id, batch.target_ref)):
                return False
        async with self.db._engine.connect() as conn:
            sources = {member.task_id: member.source_sha for member in members}
            for task_id in await _epic_branches_on(conn, ids):
                if not await self._epic_current_on(conn, task_id, requests[task_id].completion_id,
                                                   source=sources[task_id]):
                    return False
        return True

    async def closed_epic_graph(self, target):
        """Current closed-container identity, including archived/retired children."""
        async with self.db._engine.connect() as conn:
            ids = (await conn.execute(select(tasks.c.id).where(
                tasks.c.project_id == target.project_id, tasks.c.status == "COMPLETED",
                tasks.c.branch_name.in_((target.target_ref,
                                        target.target_ref.removeprefix("refs/heads/"))),
            ))).scalars().all()
            if len(ids) != 1:
                return None
            graph = await EpicGraphReader(policy_on=epic_policy_on,
                source_base_on=epic_source_base_on).read_on(conn, ids[0])
        if (not graph.node(graph.epic_id).container
                or graph.repository_id != target.repository_id or graph.project_holds
                or any(node.holds or node.task_id != graph.node(graph.epic_id).parent_id
                       and node.required and node.request.task_status != "COMPLETED"
                       for node in graph.nodes)):
            return None
        return graph

    async def sync_inputs(self, target, snapshot):
        """Freeze collected exact children; never manufacture a child or reopen one."""
        graph = await self.closed_epic_graph(target)
        if graph is None:
            return None
        if graph.epic_id in await self.delivered(target, snapshot, [graph.epic_id]):
            return None
        members, requests = [], {}
        for child in graph.children(graph.epic_id):
            request = replace(child.request, target_ref=target.target_ref)
            proof = await snapshot.is_delivered(request, source_base=child.source_base)
            if proof.state in {DeliveryState.NO_CHANGE, DeliveryState.NO_ARTIFACT}:
                continue
            if proof.state is not DeliveryState.CONTAINED or not is_valid_git_oid(proof.source_oid):
                return None
            if child.container and proof.source_oid != snapshot.for_target(child.branch_ref).target_oid:
                return None
            members.append(BatchMember(child.task_id, proof.source_oid,
                                       child.source_base or proof.source_oid))
            requests[child.task_id] = request
        return (members, requests) if members else None

    async def _epic_current_on(self, conn, task_id, generation, *, source=None,
                               snapshot=None) -> bool:
        """Recheck ordinary inputs and cached head/review verdicts at admission."""
        record = (await conn.execute(select(task_completion_records).where(
            task_completion_records.c.task_id == task_id,
            task_completion_records.c.id == generation,
        ))).mappings().one_or_none()
        if record is None or record["outcome"] != "pass":
            return False
        try:
            graph = await EpicGraphReader(policy_on=epic_policy_on,
                source_base_on=epic_source_base_on).read_on(conn, task_id)
            if snapshot is not None:
                # The completion row describes a decision; it cannot prove
                # today's children are in today's epic branch.
                async def cached_checks(repository_id, head, policy):
                    if not policy.check_names:
                        return HeadChecks(repository_id, head, (), policy.check_trust, "green")
                    rows = (await conn.execute(select(integration_check_evidence).where(
                        integration_check_evidence.c.repository_id == repository_id,
                        integration_check_evidence.c.sha == head,
                        integration_check_evidence.c.producer_id == policy.check_trust,
                        integration_check_evidence.c.required_check_version == policy.check_version,
                        integration_check_evidence.c.check_name.in_(policy.check_names),
                    ))).mappings().all()
                    green = {row["check_name"] for row in rows if row["conclusion"] == "success"}
                    return HeadChecks(repository_id, head, policy.check_names, policy.check_trust,
                                      "green" if set(policy.check_names) <= green else "unknown")

                readiness = await EpicReadinessEvaluator(self.db, EpicGraphReader(
                    policy_on=epic_policy_on, source_base_on=epic_source_base_on,
                ), TreeReviews(self.db), checks=cached_checks).evaluate(graph, snapshot)
                return readiness.ready and readiness.head_sha == source
            notes = json.loads(record["notes"])
            if source is not None and json.loads(record["commits"]) != [source]:
                return False
            if notes["graph_sha256"] != epic_graph_digest(graph):
                return False
            for subject_id, head, tree in notes["heads"]:
                policy = graph.node(subject_id).policy
                if policy.check_names:
                    green = set((await conn.execute(select(
                        integration_check_evidence.c.check_name,
                    ).where(integration_check_evidence.c.repository_id == graph.repository_id,
                        integration_check_evidence.c.sha == head,
                        integration_check_evidence.c.producer_id == policy.check_trust,
                        integration_check_evidence.c.required_check_version == policy.check_version,
                        integration_check_evidence.c.conclusion == "success"))).scalars())
                    if not set(policy.check_names) <= green:
                        return False
                review = await TreeReviews(self.db).verdict_on(conn, ReviewSubject(
                    graph.project_id, graph.repository_id, subject_id, tree), policy.reviews)
                if not review.satisfied:
                    return False
        except (ValueError, KeyError, TypeError):
            return False
        return True

    async def settle(self, batch: Batch, observation: BatchObservation) -> None:
        async with self.db._engine.connect() as conn:
            trigger = await conn.scalar(select(integration_batches.c.trigger).where(
                integration_batches.c.id == batch.id,
            ))
        async with self.db.immediate() as conn:
            if trigger == "promotion":
                from src.integration.promotion_steps import settle_promotion

                await settle_promotion(self.db, batch, observation, clock=self.clock, conn=conn)
            await conn.execute(
                update(integration_batches)
                .where(integration_batches.c.id == batch.id,
                       integration_batches.c.target_ref.is_not(None),
                       integration_batches.c.lifecycle != "promoted")
                .values(lifecycle="promoted", final_main_sha=observation.target_sha,
                        tested_candidate_sha=observation.candidate_sha,
                        updated_at=self.clock())
            )
            from src.integration.promotion_steps import promotion_policy_event

            flow = await conn.scalar(select(projects.c.promotion_flow).where(
                projects.c.id == batch.project_id))
            for step in flow or []:
                if "refs/heads/" + step["source"] == batch.target_ref:
                    await promotion_policy_event(conn, project_id=batch.project_id, step_id=step["id"],
                        kind="source_settled", identity=batch.id, now=self.clock(),
                        source_sha=observation.target_sha)
            if trigger == "promotion":
                row = await conn.scalar(select(integration_batches.c.policy_snapshot).where(
                    integration_batches.c.id == batch.id))
                step = row["promotion_step"]
                await promotion_policy_event(conn, project_id=batch.project_id, step_id=step["id"],
                    kind="delivered", identity=batch.id, now=self.clock(), batch_id=batch.id,
                    source_sha=observation.target_sha)
        if self.cleanup is not None:
            # External identity resolution is outside the settlement transaction;
            # maintenance recovers a failure between commit and materialization.
            try:
                await self.cleanup.materialize(batch.id, now=self.clock())
                await self.cleanup.advance(batch.id, now=self.clock())
            except Exception:
                logger.warning("Could not clean promoted batch %s", batch.id, exc_info=True)


async def epic_policy_on(conn, row, project) -> EpicPolicy:
    """Use current checks and admission, reusing authenticated review producers.

    The existing policy authorizes GitHub and live reviewer-task decisions,
    rather than naming a reviewer allowlist. Only those producers' decisions
    supply identities here; automatic admission never supplies a tree review.
    """
    policy = project["hierarchical_integration_policy"] or {}
    required = (policy.get("parent") or {}).get("required_checks") or {}
    boundary = "parent" if row["parent_task_id"] else "root"
    reviewed = (policy.get(boundary) or {}).get("admission") == "reviewed"
    reviewers = set()
    if reviewed:
        for identity, evidence in (await conn.execute(select(
            integration_review_evidence.c.reviewer_identity,
            integration_review_evidence.c.evidence,
        ).where(integration_review_evidence.c.source_task_id == row["id"],
                integration_review_evidence.c.repository_id == project["integration_repository_id"]
        ))).all():
            if evidence.get("decision_path") in {"github_pull_request", "review_task_close",
                                                  "reopen_with_feedback", "tree_review"}:
                reviewers.add(identity)
    trust = str(required.get("producer_id") or "")
    ci = integration_ci_policy(policy)
    if ci is not None and ci.source_for("epic") != "hosted":
        # The local runner produces the parent boundary's named checks.
        from src.integration.checks import hybrid_checks_producer_id, local_checks_producer_id

        names = tuple(required.get("names") or ())
        trust = local_checks_producer_id(
            scope="epic", names=names, version=str(required.get("version") or ""),
            commands=tuple(ci.commands[name] for name in names),
            queue_seconds=ci.queue_seconds, run_seconds=ci.run_seconds,
        )
        if ci.requires_hosted("epic"):
            trust = hybrid_checks_producer_id(trust, str(required.get("producer_id") or ""))
    return EpicPolicy(tuple(required.get("names") or ()), trust,
                      ReviewRequirements(reviewed, frozenset(reviewers)),
                      str(required.get("version") or ""))


async def epic_source_base_on(conn, row):
    return await conn.scalar(select(task_branch_origins.c.base_sha).where(
        task_branch_origins.c.task_id == row["id"],
        task_branch_origins.c.retired_at.is_(None),
    ).order_by(task_branch_origins.c.creation_generation.desc()).limit(1))


def epic_graph_digest(graph) -> str:
    """Fence the ordinary inputs a completion examined, excluding its own row."""
    data = asdict(graph)
    data["dependencies"] = [tuple(edge) for edge in graph.dependencies]
    for node in data["nodes"]:
        if node["request"]["task_id"] in {graph.epic_id, graph.node(graph.epic_id).parent_id}:
            node["request"]["completion_id"] = None
            node["request"]["completed_at"] = None
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=sorted).encode()).hexdigest()


def epic_heads(readiness):
    return [(readiness.task_id, readiness.head_sha, readiness.tree_sha),
            *(head for nested in readiness.nested for head in epic_heads(nested))]


class EpicCompletions:
    """Retain a collected epic as an ordinary exact-source completion.

    Invoked by the train command after child publication and on idle visits,
    so delayed checks/reviews and a restart need no completion notification.
    Provenance precedes the descriptive row; an interrupted write is replayed
    with the same immutable identity. No branch tip alone completes an epic.
    """

    def __init__(self, db, evaluator: EpicReadinessEvaluator, *, refresh: Callable,
                 pull_requests=None,
                 clock: Callable[[], float] = time.time):
        self.db, self.evaluator, self.refresh, self.clock = db, evaluator, refresh, clock
        self.pull_requests = pull_requests

    async def settle(self, target: TrainTarget, snapshot: GitTruthSnapshot) -> tuple[dict, ...]:
        if target.kind != "epic":
            return ()
        async with self.db._engine.connect() as conn:
            ids = (await conn.execute(select(tasks.c.id).where(
                tasks.c.project_id == target.project_id,
                tasks.c.branch_name.in_((target.target_ref, target.target_ref.removeprefix(
                    "refs/heads/"))),
                tasks.c.status == "COMPLETED",
            ))).scalars().all()
            if len(ids) != 1:
                return ()
            graph = await self.evaluator.graph_reader.read_on(conn, ids[0])
        node = graph.node(graph.epic_id)
        if not node.container or (graph.project_id, graph.repository_id) != target.key[:2]:
            return ()
        # Provider refreshes stay outside the hierarchy/action transaction.
        refused = await self.refresh(graph, snapshot) or ()
        readiness = await self.evaluator.evaluate(graph, snapshot)
        if refused or not readiness.ready:
            # A refused subject is never collected: its head checks are unread,
            # so the named refusal and the readiness verdict are reported together.
            return (*refused, *(
                {"code": reason, "task_id": task_id, "ref": task_id,
                 "detail": f"epic {node.task_id} readiness: {reason}"}
                for task_id, reason in readiness.blockers))
        # A generation never changes source. Reopening rotates legacy_generation;
        # collecting a later aggregate produces another immutable head identity.
        identity = (graph.project_id, graph.repository_id, node.task_id,
                    node.request.legacy_generation, readiness.head_sha, epic_graph_digest(graph))
        generation = "epic-" + hashlib.sha256(repr(identity).encode()).hexdigest()
        provenance = GitProvenance(snapshot.truth.git, snapshot.observation.store,
                                   repository_url=graph.repository_url)
        await provenance.write_completion(CompletedSource(CompletionIdentity(
            graph.project_id, graph.repository_id, node.task_id, generation,
        ), readiness.head_sha), claim_epoch=node.request.claim_epoch)
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, graph.project_id)
            if not await self.evaluator.current_on(conn, readiness, snapshot):
                return ({"code": "epic_changed", "task_id": node.task_id, "ref": node.task_id,
                         "detail": "epic readiness changed before completion was recorded"},)
            # The source origin is also what admits this completion to its next
            # lane. Never synthesize one from a branch that happens to exist.
            origin = await conn.scalar(select(task_branch_origins.c.id).where(
                task_branch_origins.c.task_id == node.task_id,
                task_branch_origins.c.repository_id == graph.repository_id,
                task_branch_origins.c.branch_name.in_((node.branch_ref,
                    node.branch_ref.removeprefix("refs/heads/"))),
                task_branch_origins.c.retired_at.is_(None),
            ))
            if not origin:
                return ({"code": "epic_origin_missing", "task_id": node.task_id,
                         "ref": node.task_id, "detail": "epic has no live branch origin"},)
            latest = await conn.scalar(select(task_completion_records.c.completed_at).where(
                task_completion_records.c.task_id == node.task_id,
            ).order_by(task_completion_records.c.completed_at.desc()).limit(1))
            recorded = await conn.execute(insert(task_completion_records).values(
                id=generation, task_id=node.task_id, outcome="pass",
                branch=node.branch_ref.removeprefix("refs/heads/"),
                commits=json.dumps([readiness.head_sha]),
                notes=json.dumps({"graph_sha256": epic_graph_digest(graph),
                                  "heads": epic_heads(readiness)}),
                summary="Collected epic: required children contained, head checks green, "
                        "required tree review satisfied.",
                completed_at=max(self.clock(), (latest or 0) + 0.000001),
            ).on_conflict_do_nothing(index_elements=["id"]).returning(task_completion_records.c.id))
            first = recorded.scalar_one_or_none() is not None
            from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY

            marker = insert(task_metadata).values(task_id=node.task_id,
                key=DEVELOPMENT_COMPLETION_ID_KEY, value=json.dumps(generation))
            await conn.execute(marker.on_conflict_do_update(
                index_elements=["task_id", "key"], set_={"value": marker.excluded.value}))
        if first and node.parent_id is None and self.pull_requests is not None:
            try:
                opened = await self.pull_requests.open_for_epic(
                    node.task_id, expected_head_sha=readiness.head_sha)
            except (GitError, GitHubError, GitHubAccessError, OSError, ValueError) as exc:
                opened = {"outcome": "unknown", "reason": str(exc)}
            return ({"code": "epic_pr", "task_id": node.task_id, "ref": node.task_id,
                     "head_sha": readiness.head_sha, "blocking": False, **opened},)
        return ()


class LeasedPublish:
    """``ManagedPublish`` over the per-ref lease: acquire, fenced push, release.

    A ref leased by anyone else (a repair worker on the candidate ref) refuses
    with ``BranchBusy``, which the batch service settles by reading the remote.
    """

    def __init__(self, db, git, *, holder: str = TRAIN_HOLDER, ttl_seconds: float = 120.0,
                 clock: Callable[[], float] = time.time) -> None:
        self.locks, self.git = BranchLock(db, clock=clock), git
        self.holder, self.ttl_seconds = holder, ttl_seconds
        self.clock = clock

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

    async def qualified(self, repo, target_ref, ref, *, expected_old_oid, new_oid, authorize):
        """An immutable tag write shares the promotion target's branch fence."""
        from src.integration.lock import CRITICAL_SECTION_SECONDS, PublishWithdrawn

        fence = await self.locks.acquire(
            BranchKey(repository_id=repo.repository_id, branch=target_ref), self.holder,
            ttl_seconds=self.ttl_seconds, role="integration",
        )
        try:
            async with self.locks.exclusion(fence) as row:
                if not await authorize():
                    raise PublishWithdrawn("promotion tag is no longer authorized")
                remaining = min(CRITICAL_SECTION_SECONDS, row["expires_at"] - self.clock())
                deadline = asyncio.get_running_loop().time() + remaining
                async with asyncio.timeout(remaining):
                    return await self.git.apush_qualified_ref(
                        str(repo.store), repository=repo.binding, ref=ref, tip_oid=new_oid,
                        expected_old_oid=expected_old_oid, authority_deadline=deadline,
                    )
        finally:
            await self.locks.release(fence)

    async def delete(self, repo, ref, *, expected_old_oid, authorize):
        """Cleanup of a private promotion head shares the managed-ref fence."""
        from src.integration.lock import CRITICAL_SECTION_SECONDS, PublishWithdrawn

        fence = await self.locks.acquire(
            BranchKey(repository_id=repo.repository_id, branch=ref), self.holder,
            ttl_seconds=self.ttl_seconds, role="integration",
        )
        try:
            async with self.locks.exclusion(fence) as row:
                if not await authorize():
                    raise PublishWithdrawn("promotion cleanup no longer has delivery proof")
                remaining = min(CRITICAL_SECTION_SECONDS, row["expires_at"] - self.clock())
                deadline = asyncio.get_running_loop().time() + remaining
                async with asyncio.timeout(remaining):
                    await self.git.adelete_repository_ref(
                        str(repo.store), repository=repo.binding,
                        branch=ref.removeprefix("refs/heads/"), expected_old_oid=expected_old_oid,
                        authority_deadline=deadline,
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


def _ci_kind(target) -> str:
    """The policy ``ci`` kind choosing *target*'s candidate runner."""
    return target.kind if target.kind in {"epic", "promotion"} else "root"


async def _exact_head_verdict(exact, head):
    """Re-observe hosted checks; only read a local runner's stored rows.

    A local job exists once requested for its exact head and plan. Observing a
    head nobody requested would overwrite the evidence its own candidate run
    left there with ``not_requested``.
    """
    from src.integration.checks import HostedChecks, HybridChecks

    if isinstance(exact, HybridChecks):
        if exact.hosted is not None:
            await exact.hosted.refresh(head)
        return await exact.read(head)
    if isinstance(exact.provider, HostedChecks):
        return await exact.refresh(head)
    return await exact.read(head)


class DaemonLanes:
    """Per-target lanes over the daemon's git, retained stores and checks.

    The store is the repository's retained clone. A project with a pinned
    development source validates its root candidates with that source's local
    jobs and rebuilds generated files with its command. Otherwise the project
    policy's ``ci`` block picks each target kind's runner (``hosted`` when
    unset): a candidate gate, the root PR gate and a promotion step gate each
    read the boundary's named required checks from that one producer.
    """

    def __init__(self, orchestrator, *, batches: DatabaseBatches,
                 clock: Callable[[], float] = time.time,
                 review_requirements: ReviewRequirements | None = None) -> None:
        self.orchestrator, self.batches, self.clock = orchestrator, batches, clock
        self.db, self.git = orchestrator.db, orchestrator.git
        self.truth = GitTruth(orchestrator.git, share_fetches=True)
        self.store = BatchStore(self.db, clock=clock)
        self.publish = LeasedPublish(self.db, self.git, clock=clock)
        if self.batches is not None and self.batches.pr_gate is None:
            from src.integration.github_review_poll import RootPullRequestGate

            async def repository(target):
                repo = await self.db.get_repo(target.repository_id)
                binding = await self.orchestrator.github_repository_binding_resolver(repo)
                return binding, self.git._github_client(binding)

            hosted_pr_gate = RootPullRequestGate(
                self.db, repository=repository, checks=self._pr_checks, clock=clock,
                review_requirements=review_requirements,
                failure_handler=getattr(self.orchestrator, "_repair_root_pr_checks", None))

            async def pr_gate(target, member):
                # Local candidate validation comes from a retained reviewed
                # artifact. Its admission does not depend on hosted PRs.
                settings, _ = await self._settings(await self._policy(target.project_id))
                if settings is not None:
                    return None
                return await hosted_pr_gate(target, member)

            self.batches.pr_gate = pr_gate
            self.batches.repaired_source_gate = hosted_pr_gate.admit_repaired_source
        #: batch id -> (key, expires_at, trust, client); see _promotion_trust.
        self._promotion_trust_cache: dict[str, tuple[str, float, Any, Any]] = {}

    async def __call__(self, target: TrainTarget) -> TrainLane:
        from src.integration.development_runtime import development_repository
        from src.integration.train import _progress

        policy = await self._policy(target.project_id)
        repair_policy = (policy.get("parent" if target.kind == "epic" else "root") or {}).get(
            "repair"
        )
        settings, version = await self._settings(policy)
        repo_row = await self.db.get_repo(target.repository_id)
        _progress("repository_binding")
        binding = await self.orchestrator.github_repository_binding_resolver(repo_row)
        primitives = self.orchestrator.development_integration
        _progress("retained_store")
        # snapshot() below owns the fetch. Fetching here as well queues two
        # serialized repository-wide fetches per target after every restart.
        if settings is not None:
            retained = await development_repository(
                primitives, repo_row, binding, settings, fetch=False)
        else:
            # A non-development project still merges generated artifacts; the
            # lane rebuilds them with the project's own regenerator.
            retained = RetainedRepository(
                repository_id=target.repository_id,
                store=await primitives.store(repo_row, fetch=False),
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
        # A development source's own local jobs take precedence; its policy
        # has no ci block.
        ci = None if settings is not None else integration_ci_policy(policy)
        local_ci = (ci is not None and target.kind != "promotion"
                    and ci.source_for(_ci_kind(target)) != "hosted")

        async def resolve(batch: Batch, candidate_sha: str):
            if local:
                return await self._local(target, retained, settings, version, batch,
                                         candidate_sha)
            if local_ci:
                await retain_train_candidate(self.git, retained.store, batch, candidate_sha)
                return await self._policy_checks(target, retained, binding, ci, policy, batch,
                                                 candidate_sha)
            return await self._hosted(policy, binding, target, batch, candidate_sha,
                                      retained=retained)

        checks = CandidateChecks(
            resolve, advisory=local and settings.validation == "advisory"
        )

        async def attest(batch: Batch, candidate_sha: str) -> str:
            from src.integration.ci_producers import HostedCIProducer, LocalCIProducer
            from src.integration.promotion_contracts import RootAttestationSubject

            attestation = getattr(self.orchestrator, "integration_attestation_service", None)
            if attestation is None:
                return "unavailable"
            exact = await checks.for_candidate(batch, candidate_sha)
            from src.integration.checks import HybridChecks

            if isinstance(exact, HybridChecks):
                exact = exact.hosted
            producer = None if exact is None else exact.provider.producer
            if isinstance(producer, LocalCIProducer) or not isinstance(producer, HostedCIProducer):
                return "unavailable"
            subject = RootAttestationSubject(
                repository_numeric_id=binding.repository_id, repository_full_name=binding.full_name,
                operation_id=batch.id, batch_id=batch.id, revision=batch.repair_attempt_count,
                candidate_sha=candidate_sha, required_check_version=exact.required.version,
            )
            return (await attestation.publish(subject, producer=producer)).outcome

        async def snapshot() -> GitTruthSnapshot:
            return await self.truth.snapshot(
                str(retained.store), project_id=target.project_id,
                repository_id=target.repository_id, repository_url=repo_row.url,
                target_ref=target.target_ref,
            )

        if target.kind == "promotion":
            return await self._promotion_lane(target, retained, binding, gitops, policy, snapshot,
                                              ci=ci)

        unattested = local or (local_ci and not ci.requires_hosted(_ci_kind(target)))
        service = BatchService(self.store, gitops, publish=self.publish,
                               eligible=self.batches.eligible, gate=checks.gate,
                               attest=None if unattested else attest,
                               require_attestation=not unattested)

        complete_epic = None
        sync_default_branch = None
        sync_closed_epic = None
        if target.kind == "epic":
            complete_epic = self._epic_completion(target, binding, retained)

            async def sync_default_branch(batch, snapshot, candidate_sha, names):
                return await self._default_branch_sync(
                    target, retained, binding, policy, batch, snapshot, candidate_sha, names,
                )

            async def sync_closed_epic(snapshot):
                if snapshot.error or not snapshot.target_oid:
                    return None
                inputs = await self.batches.sync_inputs(target, snapshot)
                if inputs is None:
                    return None
                members, requests = inputs
                identity = (target.key, snapshot.target_oid, tuple(
                    (member.task_id, member.source_sha) for member in members))
                batch = Batch("train-epic-sync-" + hashlib.sha256(repr(identity).encode()).hexdigest(),
                              target.project_id, target.repository_id, target.target_ref,
                              created_at=self.clock())
                try:
                    exact = await resolve(batch, snapshot.target_oid)
                    if exact is None:
                        return None
                    from src.integration.checks import Conclusion
                    from src.integration.runtime_contracts import HeadIdentity

                    result = await _exact_head_verdict(exact, HeadIdentity(
                        repository_id=target.repository_id, ref=target.target_ref,
                        sha=snapshot.target_oid, generation=0,
                    ))
                    names = tuple(check.name for check in result.checks
                                  if check.conclusion is Conclusion.FAILURE)
                    if not names or not await sync_default_branch(
                            batch, snapshot, snapshot.target_oid, names):
                        return None
                    frozen = await service.freeze(batch, members, requests=requests, snapshot=snapshot)
                    return BatchSelection(frozen, await self.store.members(frozen.id))
                except (AttestationError, GitHubAccessError, GitError, OSError, ValueError):
                    return None

        return TrainLane(snapshot=snapshot, service=service, checks=checks,
                         repair_policy=(RepairPolicy.model_validate(repair_policy)
                                        if repair_policy else None),
                         complete_epic=complete_epic, sync_default_branch=sync_default_branch,
                         sync_closed_epic=sync_closed_epic)

    async def _default_branch_sync(
        self, target, retained, binding, policy, batch, snapshot, candidate_sha, names,
    ) -> dict[str, Any] | None:
        """An epic may merge only a verified current default head, once per batch.

        Read the default ref from the same fetched snapshot as the candidate.
        Its own policy and exact-tree trust govern fresh hosted checks. A known
        root batch's fetched candidate at that same OID proves train production;
        otherwise require the trusted App's exact-head attestation. Neither
        cached green alone nor ancestry of an older green head suffices.
        """
        default_ref = _branch(retained.default_branch)
        if target.kind != "epic" or target.target_ref == default_ref or not names:
            return None
        default = snapshot.for_target(default_ref)
        sha = default.target_oid
        if default.error or not is_valid_git_oid(sha):
            return None
        try:
            if await self.git.ais_ancestor(str(retained.store), sha, candidate_sha, strict=True):
                # The proposed fix is already in this candidate. Repeating the
                # merge cannot repair it, even on a new repair generation.
                return None
            root = TrainTarget(target.project_id, target.repository_id, default_ref)
            ci = integration_ci_policy(policy)
            if ci is not None and ci.source_for("root") != "hosted":
                # The root candidate that became this head left its local
                # evidence; reading it runs nothing.
                exact = await self._policy_checks(root, retained, binding, ci, policy, batch, sha)
            else:
                exact = await self._hosted(policy, binding, root, batch, sha)
            if exact is None or not set(names) <= set(exact.required.names):
                return None
            from src.integration.checks import Conclusion
            from src.integration.runtime_contracts import HeadIdentity

            result = await _exact_head_verdict(exact, HeadIdentity(
                repository_id=target.repository_id, ref=default_ref, sha=sha,
                generation=batch.repair_attempt_count,
            ))
            rows = {check.name: check for check in result.checks}
            if any(rows[name].conclusion is not Conclusion.SUCCESS for name in names):
                return None
            async with self.db._engine.connect() as conn:
                root_batches = (await conn.execute(select(integration_batches.c.id).where(
                    integration_batches.c.project_id == target.project_id,
                    integration_batches.c.repository_id == target.repository_id,
                    integration_batches.c.target_ref == default_ref,
                ))).scalars().all()
            produced = any(snapshot.for_target(candidate_ref(id_)).target_oid == sha
                           for id_ in root_batches)
            provenance = "train_candidate"
            if not produced:
                # Only a hosted head carries the App's attestation.
                from src.integration.checks import HybridChecks

                hosted = exact.hosted if isinstance(exact, HybridChecks) else exact
                if hosted is None:
                    return None
                trust = getattr(hosted.provider.producer, "trust", None)
                if not isinstance(trust, IntegrationTrustManifest):
                    return None
                producer = hosted.provider.producer
                attestation = self.orchestrator.integration_attestation_service
                records = await attestation._attestation_records(producer.client, trust, sha)
                select_trusted_attestation(records, trust, expected_head_sha=sha)
                provenance = "attestation"
            merged = await self.git.arun_git_result(
                ["--no-replace-objects", "merge-tree", "--write-tree", "--name-only", "-z",
                 candidate_sha, sha], cwd=str(retained.store),
            )
            if merged.returncode not in {0, 1}:
                raise GitError(merged.stderr or "default sync merge inspection failed")
            decision = {"ref": default_ref, "sha": sha, "checks": sorted(names),
                        "provenance": provenance}
            if merged.returncode:
                files = merged.stdout.split("\0\0", 1)[0].split("\0")[1:]
                if not files:
                    raise GitError("default sync conflict inspection did not name files")
                decision["conflicting_files"] = sorted(set(files))
            return decision
        except (AttestationError, GitHubAccessError, GitError, OSError, ValueError):
            # Unavailable or invalid default evidence never weakens the epic's
            # existing human blocker or spends a repair attempt.
            logger.info("integration batch %s default-branch sync evidence unavailable", batch.id)
            return None

    def _epic_completion(self, target, binding, retained):
        from src.integration.checks import HostedChecks, HybridChecks
        from src.integration.runtime_contracts import HeadIdentity

        resolved = {}

        async def refresh(graph, snapshot):
            policy = await self._policy(target.project_id)
            ci = integration_ci_policy(policy)
            local = ci is not None and ci.source_for("epic") != "hosted"
            refused: list[dict] = []
            for node in graph.nodes:
                head = snapshot.for_target(node.branch_ref).target_oid if node.branch_ref else None
                if not node.container or not head:
                    continue
                if not node.policy.check_names:
                    resolved[(head, node.policy)] = None
                    continue
                batch = Batch("epic-readiness-" + node.task_id, target.project_id,
                              target.repository_id, node.branch_ref)
                try:
                    if local:
                        await retain_train_candidate(self.git, retained.store, batch, head)
                        exact = await self._policy_checks(target, retained, binding, ci, policy,
                                                         batch, head)
                    else:
                        exact = await self._hosted(policy, binding, target, batch, head)
                except SubjectTrustError as exc:
                    # A branch cut before the repository carried a trust manifest
                    # owes the epic a refresh, not a failed visit: the head's
                    # checks stay unread and the refusal is named.
                    resolved[(head, node.policy)] = None
                    refused.append({
                        "code": "subject_trust_missing" if exc.cause == "missing"
                        else "subject_trust_invalid",
                        "task_id": node.task_id, "ref": node.branch_ref, "head_sha": head,
                        "detail": f"epic {node.task_id} branch {node.branch_ref} at {head}: "
                                  f"{exc}; refresh the branch from the default branch",
                    })
                    continue
                resolved[(head, node.policy)] = exact
                if exact is not None:
                    identity = HeadIdentity(repository_id=graph.repository_id,
                                            ref=node.branch_ref, sha=head, generation=0)
                    # A published epic head is usually the candidate its train
                    # already ran locally; green evidence there runs nothing.
                    if isinstance(exact, HybridChecks):
                        if exact.hosted is not None:
                            await exact.hosted.refresh(identity)
                        if not (await exact.local.read(identity)).green:
                            await exact.local.request(identity)
                            await exact.local.refresh(identity)
                    elif (isinstance(exact.provider, HostedChecks)
                            or not (await exact.read(identity)).green):
                        await exact.request(identity)
                        await exact.refresh(identity)
            return tuple(refused)

        async def head_checks(repository_id, head, policy):
            exact = resolved.get((head, policy))
            if exact is None:
                return HeadChecks(repository_id, head, policy.check_names, policy.check_trust,
                                  "unknown" if policy.check_names else "green")
            result = await exact.read(HeadIdentity(repository_id=repository_id,
                ref=target.target_ref, sha=head, generation=0))
            state = result.state.value if exact.required.version == policy.check_version else "unknown"
            return HeadChecks(repository_id, head, tuple(result.required.names),
                              result.required.producer_id, state)

        evaluator = EpicReadinessEvaluator(self.db, EpicGraphReader(
            policy_on=epic_policy_on, source_base_on=epic_source_base_on,
        ), TreeReviews(self.db), checks=head_checks)
        from src.integration.root_pull_requests import EpicPullRequestService

        completions = EpicCompletions(self.db, evaluator, refresh=refresh, clock=self.clock,
            pull_requests=EpicPullRequestService(self.db, git_manager=self.git, clock=self.clock))

        async def complete(snapshot):
            return await completions.settle(target, snapshot)

        return complete

    async def _promotion_lane(self, target, retained, binding, gitops, policy, snapshot, *,
                              ci=None):
        from src.integration.checks import ExactChecks, HostedChecks
        from src.integration.ci import IntegrationTrustManifest
        from src.integration.ci_producers import HostedCIProducer
        from src.integration.promotion_steps import (
            PromotionChecks,
            PromotionIntentInvalid,
            PromotionVisit,
            StepAdmission,
            StepPullRequestGate,
            frozen_required_checks,
            publish_step_attestation,
        )

        admission = StepAdmission(self.db, gitops, target.step)
        resolved = {}
        local = ci is not None and ci.source_for("promotion") == "local"

        async def resolve_local(batch, sha, meta):
            # S's manifest must still name this repository and App identity, but
            # the check set is the one the request froze from committed trust,
            # never S's own manifest (attestation I3). This box runs them, and
            # no App proof is published.
            manifest = await self._retained_manifest(retained, sha, binding=binding, policy=policy)
            required = frozen_required_checks(meta)
            missing = sorted(set(required.names) - set(ci.commands))
            if missing:
                raise PromotionIntentInvalid("ci_command_missing:" + ",".join(missing))
            client = self.git._github_client(binding)
            resolved[batch.id] = manifest, client, StepPullRequestGate(client, binding)
            await retain_train_candidate(self.git, retained.store, batch, sha)
            return self._local_checks(target, retained.store, ci, None, batch, requested=True,
                                      names=required.names, version=required.version)

        async def resolve(batch, sha):
            members = await self.store.members(batch.id)
            meta = await admission.load(batch, members)
            if local:
                return await resolve_local(batch, sha, meta)
            attestation = getattr(self.orchestrator, "integration_attestation_service", None)
            if attestation is None:
                raise PromotionIntentInvalid("promotion requires hosted App attestation service")
            state = {
                "project_id": target.project_id, "canonical_repository_id": target.repository_id,
                "repository_numeric_id": binding.repository_id,
                "repository_full_name": binding.full_name, "candidate_sha": sha,
                "batch_id": batch.id, "operation_id": batch.id,
                "revision": batch.repair_attempt_count,
                "policy_snapshot": {"root": policy.get("root") or {}},
            }
            # The App identity checks refuse a subject naming another identity;
            # the check set is the one the request froze from committed trust,
            # never S's own manifest (attestation I3).
            trust, client = await self._promotion_trust(attestation, batch, state)
            if not isinstance(trust, IntegrationTrustManifest):
                raise PromotionIntentInvalid("promotion requires App-mode trust")
            required = frozen_required_checks(meta)
            resolved[batch.id] = trust, client, StepPullRequestGate(client, binding)
            selected = trust.model_copy(update={"required_checks": required})
            return ExactChecks(self.db, HostedChecks(HostedCIProducer(client, selected)))

        checks = PromotionChecks(admission, resolve, pull_request=lambda batch: resolved[batch.id][2])

        async def attest(batch, sha):
            trust, client, _pr_gate = resolved[batch.id]
            return await publish_step_attestation(
                admission, batch, await self.store.members(batch.id), trust, client,
            )

        service = PromotionVisit(
            self.store, gitops, admission=admission, checks=checks, publish=self.publish,
            publish_tag=self.publish.qualified, delete_ref=self.publish.delete,
            attest=None if local else attest, snapshot=snapshot, clock=self.clock,
        )
        await service.reconcile_cleanup(target)

        async def observe_source(observed):
            await self._observe_promotion_source(target, retained, gitops, observed)

        return TrainLane(snapshot=snapshot, service=service, checks=checks,
                         observe_source=observe_source if target.step["type"] == "continuous" else None)

    async def _observe_promotion_source(self, target, retained, gitops, snapshot):
        """Notify reviewed continuous policy when exact source push CI becomes green."""
        from src.integration.promotion_steps import (
            FlowSchema, PromotionSourceRefusal, check_source_green, promotion_policy_event,
            step_required_checks,
        )
        from src.playbooks.services import resolve_integration_route

        if snapshot.error or not snapshot.target_oid:
            return
        source = snapshot.observation.source_heads.get(
            "refs/remotes/origin/" + target.step["source"])
        if not is_valid_git_oid(source):
            return
        if await gitops.is_ancestor(retained, source, snapshot.target_oid):
            return
        route = await resolve_integration_route(self.db, "promotion.source_settled", {
            "project_id": target.project_id, "step_id": target.step["id"],
        })
        if route is None:
            return
        digest = hashlib.sha256(json.dumps(
            [target.step, route.artifact_sha256], sort_keys=True).encode()).hexdigest()
        identity = f"source-ready:{target.repository_id}:{source}:{digest}"
        async with self.db._engine.connect() as conn:
            flow = await conn.scalar(select(projects.c.promotion_flow).where(
                projects.c.id == target.project_id,
                projects.c.integration_repository_id == target.repository_id))
        # The source cannot supply its own trust anchors or check set.
        default = snapshot.observation.source_heads.get(
            "refs/remotes/origin/" + retained.default_branch)
        trust = await self._retained_manifest(
            retained, default, binding=retained.binding, policy=None)
        validated = FlowSchema.validate(flow, default_branch=retained.default_branch,
                                        manifest=trust.model_dump(mode="json", by_alias=True))
        if not validated.valid or target.step not in validated.flow:
            return
        client = self.git._github_client(retained.binding)
        try:
            await check_source_green(client, trust, step_required_checks(trust, target.step), source)
        except PromotionSourceRefusal:
            # A one-shot settlement can precede push CI. Leave it eligible for
            # the next level-triggered visit instead of consuming its retry.
            return
        async with self.db.immediate() as conn:
            current = await conn.scalar(select(projects.c.promotion_flow).where(
                projects.c.id == target.project_id,
                projects.c.integration_repository_id == target.repository_id).with_for_update())
            if current != flow:
                return
            await promotion_policy_event(conn, project_id=target.project_id,
                step_id=target.step["id"], kind="source_settled", identity=identity,
                now=self.clock(), source_sha=source)

    async def _promotion_trust(self, attestation, batch, state):
        """The visit's subject trust, reused while S, revision and policy are unchanged."""
        key = json.dumps([state["candidate_sha"], state["revision"],
                          state["policy_snapshot"]], sort_keys=True, default=str)
        now = self.clock()
        cache = self._promotion_trust_cache
        for batch_id in [b for b, entry in cache.items() if entry[1] <= now]:
            del cache[batch_id]
        cached = cache.get(batch.id)
        if cached is not None and cached[0] == key:
            trust, client = cached[2], cached[3]
        else:
            trust, client = await attestation._load_trust(state)
        cache[batch.id] = (key, now + PROMOTION_RESOLUTION_TTL_SECONDS, trust, client)
        return trust, client

    async def _pr_checks(self, target, member, policy, binding):
        """The required checks a root PR member's head must pass to be admitted."""
        batch = Batch("pr-admission-" + member.task_id, target.project_id,
                      target.repository_id, target.target_ref)
        ci = integration_ci_policy(policy)
        if ci is not None and ci.source_for("root") == "local":
            # The member head's own required checks, run on this box; hybrid
            # still reads the PR checks GitHub enforces.
            repo_row = await self.db.get_repo(target.repository_id)
            primitives = self.orchestrator.development_integration
            store = await primitives.store(repo_row, fetch=False)
            try:
                await retain_train_candidate(self.git, store, batch, member.source_sha)
            except ValueError:
                # The member head was pushed after the store's last fetch.
                await primitives.store(repo_row)
                await retain_train_candidate(self.git, store, batch, member.source_sha)
            return self._local_checks(target, store, ci, policy, batch, requested=True)
        return await self._hosted(policy, binding, target, batch, member.source_sha,
                                  expected_event="pull_request")

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

    async def _retained_manifest(self, retained, sha, *, binding, policy):
        """Read bound trust at SHA; policy=None is the trusted default branch.

        Candidate trees must match the boundary's configured CI producer. The
        default branch itself supplies promotion trust, as in promote_request.
        """
        from src.integration.attestation import _MAX_TRUST_BYTES, _parse_trust_manifest
        from src.integration.ci import TRUST_MANIFEST_PATH
        from src.git.github_contracts import GitHubCredentialMode, credential_identity_from_client

        if not is_valid_git_oid(sha):
            raise SubjectTrustError("identity_mismatch", "subject head is not a Git OID")

        result = await self.git.arun_git_result(
            ["show", f"{sha}:{TRUST_MANIFEST_PATH}"], cwd=str(retained.store),
            env={"LC_ALL": "C"},
        )
        if result.returncode:
            raise SubjectTrustError("missing", f"subject tree has no {TRUST_MANIFEST_PATH}")
        raw = result.stdout.encode("utf-8")
        if len(raw) > _MAX_TRUST_BYTES:
            raise SubjectTrustError(
                "too_large", f"subject trust manifest exceeds {_MAX_TRUST_BYTES} bytes"
            )
        manifest = _parse_trust_manifest(raw)
        identity = credential_identity_from_client(self.git._github_client(binding))
        expected = {
            "canonical_repository_id": retained.repository_id,
            "repository_id": binding.repository_id,
            "full_name": binding.full_name,
        }
        if policy is not None:
            expected["ci_producer_app_id"] = str(
                (policy.get("root") or {}).get("required_checks", {}).get("producer_id") or "")
        if identity.mode is GitHubCredentialMode.APP:
            expected["attestation_app_id"] = identity.app_id
        mismatched = tuple(field for field, value in expected.items()
                           if str(getattr(manifest, field)) != str(value))
        if mismatched:
            raise SubjectTrustError(
                "identity_mismatch", "subject trust manifest names another identity: "
                + ", ".join(mismatched), fields=mismatched,
            )
        return manifest

    async def _policy_checks(self, target, retained, binding, ci, policy, batch, sha):
        """Local gate, plus hosted evidence when this hybrid boundary requires it."""
        from src.integration.checks import HybridChecks

        local = self._local_checks(target, retained.store, ci, policy, batch)
        if not ci.requires_hosted(_ci_kind(target)):
            return local
        hosted = await self._hosted(policy, binding, target, batch, sha, retained=retained)
        boundary = "parent" if target.kind == "epic" else "root"
        producer_id = str((policy.get(boundary) or {}).get("required_checks", {})
                          .get("producer_id") or "")
        return HybridChecks(local, hosted, hosted_producer_id=producer_id)

    def _local_checks(self, target, store, ci, policy, batch, *, requested=False,
                      names=None, version=None):
        """The local runner producing a boundary's named required checks.

        Each required check name runs its ``ci.commands`` entry and its row
        carries that name under the boundary's version: the same checks a hosted
        runner reports, from another producer. *names* and *version* default to
        the target boundary's ``required_checks`` in *policy*.
        """
        from src.integration.checks import ExactChecks, LocalChecks, RequestedChecks
        from src.integration.ci_producers import LocalCIProducer, LocalValidationPlan
        from src.jobs.adapters import PublisherJobs

        if names is None:
            boundary = "parent" if target.kind == "epic" else "root"
            required = (policy.get(boundary) or {}).get("required_checks") or {}
            names, version = required.get("names") or (), str(required.get("version") or "")
        names = tuple(names)
        missing = sorted(set(names) - set(ci.commands))
        if missing:
            raise ValueError("ci.commands has no command for required check(s) "
                             + ", ".join(missing))
        plan = LocalValidationPlan(
            version=version, attempt_id=f"{batch.repair_attempt_count}:0",
            commands=tuple(ci.commands[name] for name in names),
            queue_seconds=ci.queue_seconds, run_seconds=ci.run_seconds,
        )
        producer = LocalCIProducer(
            self.db, PublisherJobs(lambda: self.orchestrator._command_handler),
            store=store, plan=plan, clock=self.clock,
        )
        checks = RequestedChecks if requested else ExactChecks
        return checks(self.db, LocalChecks(producer, project_id=target.project_id,
                                           names=names, version=version,
                                           scope=("promotion:" + str(target.step_id)
                                                  if target.kind == "promotion"
                                                  else _ci_kind(target))), clock=self.clock)

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

    async def _hosted(self, policy, binding, target, batch, candidate_sha, *, retained=None,
                      expected_event="push"):
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

        async def diagnose(head):
            return await self._workflow_diagnostic(retained, head)

        return ExactChecks(self.db, HostedChecks(
            HostedCIProducer(client, trust, expected_event=expected_event),
            diagnose=diagnose if retained is not None else None,
        ), clock=self.clock)

    async def _workflow_diagnostic(self, retained, head):
        """Read workflow filters from the candidate's tree, never the daemon checkout."""
        async def read(args):
            result = await self.git.arun_git_result(args, cwd=str(retained.store))
            if result.returncode:
                raise GitError(result.stderr.strip())
            return result.stdout

        paths = await read(["ls-tree", "-r", "--name-only", head.sha, "--", ".github/workflows"])
        workflows, reasons = [], []
        for path in paths.splitlines():
            if not path.endswith((".yml", ".yaml")):
                continue
            source = await read(["show", f"{head.sha}:{path}"])
            try:
                document = yaml.safe_load(source)
            except yaml.YAMLError:
                reasons.append(f"{path}: workflow YAML is malformed.")
                continue
            if not isinstance(document, dict):
                reasons.append(f"{path}: workflow YAML is not a mapping.")
                continue
            # PyYAML's YAML 1.1 loader treats an unquoted `on` as True.
            events = document.get("on", document.get(True, {}))
            has_push = ("push" in events if isinstance(events, (dict, list))
                        else events == "push")
            push = events.get("push") if isinstance(events, dict) else None
            push = push if isinstance(push, dict) else {}
            allowed = _push_branch_allowed(push, head.ref) if has_push else False
            filters = {key: push[key] for key in
                       ("branches", "branches-ignore", "tags", "tags-ignore", "paths", "paths-ignore")
                       if key in push}
            workflows.append({"path": path, "name": document.get("name", path),
                              "push": has_push, "branch_allowed": allowed, "filters": filters})
            if allowed is False:
                reasons.append(f"{path}: " + (
                    f"push trigger filters {json.dumps(filters, sort_keys=True)} exclude {head.ref}."
                    if has_push else "no push trigger."
                ))
            elif allowed is None:
                reasons.append(f"{path}: push trigger filters {json.dumps(filters, sort_keys=True)} "
                               f"require inspection for {head.ref}.")
        if not workflows and not reasons:
            reasons.append("The candidate tree has no .github/workflows YAML files.")
        if not reasons:
            reasons.append("Candidate workflows allow the branch; inspect path filters, "
                           "push credentials and GitHub Actions availability.")
        return {"workflows": workflows, "reason": " ".join(reasons),
                "repair_source_ref": "refs/heads/" + retained.default_branch}


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

    batches = DatabaseBatches(orchestrator.db, clock=clock,
                             cleanup=getattr(orchestrator, "integration_cleanup_service", None))
    return IntegrationTrain(
        targets=DatabaseTargets(
            orchestrator.db,
            flow_problems=lambda: getattr(orchestrator, "promotion_flow_problems", None),
        ),
        batches=batches,
        lane_for=DaemonLanes(orchestrator, batches=batches, clock=clock),
        repair=OrdinaryRepairService(orchestrator.db, clock=clock),
        baseline=CandidateBaselineService(orchestrator.db, clock=clock),
        clock=clock,
    )
