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
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import asdict, replace
from pathlib import Path
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
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.batches import (
    Batch,
    BatchMember,
    BatchObservation,
    BatchService,
    BatchStore,
    SupersedeMemberUnavailable,
    candidate_ref,
    pair_conflict_sources_query,
    released_repair_attempts_on,
    withheld_source_blocker,
    withheld_sources_query,
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
    DeliveryState,
    load_delivery_requests,
    superseded_source_repairs_on,
    undeliverable_repairs_on,
)
from src.integration.epics import (
    EpicGraphReader,
    EpicPolicy,
    EpicReadinessEvaluator,
    HeadChecks,
    playbook_epic_checks_on,
)
from src.integration.git_truth import GitTruth, GitTruthSnapshot
from src.integration.gitops import GitOperations, RetainedRepository, SubjectGitAuthority
from src.integration.lock import BranchLock
from src.integration.manual_control import train_write
from src.integration.models import (
    BranchKey,
    RepairPolicy,
    integration_ci_policy,
)
from src.integration.promotion_steps import flow_status, flow_targets
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.regeneration import DEFAULT_REGENERATE_COMMAND, generated_paths
from src.integration.reviews import ReviewRequirements, ReviewSubject, TreeReviews
from src.integration.runtime_contracts import Subject
from src.integration.selection_metrics import selection_count, selection_stage
from src.integration.train import (
    BatchSelection,
    CandidateChecks,
    IntegrationTrain,
    TrainLane,
    TrainTarget,
)
from src.projects.github import GitHubError
from src.waits.sources import execute_batch_mutation, execute_task_status_mutation

logger = logging.getLogger(__name__)

TRAIN_MODES = ("train", "hierarchy", "development")
TRAIN_HOLDER = "service:integration-train"
# A local branch keeps a candidate reachable from the retained store, so a
# detached validation clone sees it. Never pushed.
RETAINED_CANDIDATE_PREFIX = "refs/heads/aq/train-candidate/"
MEMBER_LIMIT = 200
# Leave time in the visit deadline to freeze and advance an admitted prefix: a
# cooperative quantum (not a fenced writer's deadline) that red or conflicting roots do not spend.
SELECTION_WORK_SECONDS = 60.0
_REFUNDED_PR_REFUSALS = frozenset({"pr_conflicting", "pr_checks_red", "source_ci_repair_exhausted", "source_ci_repair_unchanged",
    "root_pr_checks_need_repair", "root_pr_checks_unchanged", "root_pr_checks_recovery_exhausted"})
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
    """Completed tasks with a known filing base, preferring their live origin."""
    from src.integration.promotion_routing import cutover_origin_clause

    flow, cutover = (await conn.execute(select(projects.c.promotion_flow,
        projects.c.default_branch_cutover).where(projects.c.id == project_id))).one()
    promotion_refs = {
        ref for name in flow_targets(flow) for ref in (name, _branch(name))
    }
    retained = task_branch_origins.alias("pending_retained_origin")
    latest_origin = select(retained.c.id).where(
        retained.c.task_id == tasks.c.id,
        retained.c.repository_id == repository_id,
    ).order_by(retained.c.retired_at.is_(None).desc(),
               retained.c.creation_generation.desc()).limit(1).correlate(tasks).scalar_subquery()
    rows = await conn.execute(
        select(tasks.c.id)
        .select_from(tasks.join(task_branch_origins, and_(
            task_branch_origins.c.task_id == tasks.c.id,
            task_branch_origins.c.repository_id == repository_id,
            task_branch_origins.c.id == latest_origin,
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

async def _epic_branches_on(conn, ids=(), *, target=None):
    """Select containers by task IDs or a completed target, including archived children."""
    from src.database.queries.hierarchy_queries import container_flag_exists
    from src.routing.sources import ROLE

    identity = tasks.c.id.in_(ids) if target is None else and_(
        tasks.c.project_id == target.project_id, tasks.c.status == "COMPLETED",
        or_(tasks.c.repo_id == target.repository_id, tasks.c.repo_id.is_(None)),
        tasks.c.branch_name.in_((target.target_ref, target.target_ref.removeprefix("refs/heads/"))),
        tasks.c.route_source != ROLE,
    )
    child, archived = tasks.alias("epic_child"), archived_tasks.alias("epic_archived_child")
    return dict((await conn.execute(select(tasks.c.id, tasks.c.branch_name).where(
        identity, tasks.c.branch_name.is_not(None),
        container_flag_exists() | select(child.c.id).where(
            child.c.parent_task_id == tasks.c.id).exists() | select(archived.c.id).where(
            archived.c.parent_task_id == tasks.c.id).exists(),
    ).order_by(tasks.c.id))).all())


async def project_snapshot(db, target, *, read=False, cached_only=False) -> GitTruthSnapshot | None:
    """Observe the project root through the daemon's isolated delivery store."""
    from src.integration.delivery_observer import DeliveryObserver, prerequisite_observer

    observer = prerequisite_observer(db) or getattr(db, "_delivery_observer", None)
    if observer is None:
        return None
    repo = await db.get_repo(target.repository_id)
    observed = await observer.snapshot(DeliveryTarget(
        target.project_id, target.repository_id, repo.url, target.target_ref,
    ), max_age=DeliveryObserver.READ_MAX_AGE if read else 0.0, cached_only=cached_only)
    return observed if isinstance(observed, GitTruthSnapshot) else GitTruthSnapshot(
        GitTruth(observer.git), observed, cached_only=cached_only,
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
        if (request is None or request.task_status != "COMPLETED"
            or request.lifecycle_disposition is not None):
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
        self.retired_targets: set[tuple[str, str, str]] = set()
        self.deferred_revisits: dict[tuple[str, str, str], float] = {}

    async def targets(self, now: float, *, project_id: str | None = None) -> list[TrainTarget]:
        # A scoped read (one control's own project) probes only that project's
        # origin and leaves the fleet-wide retirement set to the tick.
        found: dict[tuple[str, str, str], TrainTarget] = {}
        retired = self.retired_targets if project_id is None else set()
        retired.clear()
        deferred = self.deferred_revisits if project_id is None else {}
        deferred.clear()
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(
                select(projects.c.id, projects.c.hierarchical_integration_mode,
                       projects.c.promotion_flow,
                       repos.c.id.label("repository_id"), repos.c.default_branch)
                .select_from(projects.join(
                    repos, repos.c.id == projects.c.integration_repository_id))
                .where(projects.c.hierarchical_integration_mode.in_(TRAIN_MODES),
                       projects.c.status == "ACTIVE",
                       *([projects.c.id == project_id] if project_id else []))
                .order_by(projects.c.id)
            )).mappings().all()
        from src.integration.train_policy import load_train_policy

        active = []
        for row in rows:
            if (await load_train_policy(self.db, row["id"])).reason != "train_playbook_disabled":
                active.append(row)
        rows = active
        # Root probes run concurrently and outside a held database connection.
        # A stalled probe leaves work owed; visits perform their own root proof.
        for targets in await asyncio.gather(*(
                self._project_targets(row, retired, deferred) for row in rows)):
            for target in targets:
                found.setdefault(target.key, target)
        return [found[key] for key in sorted(found)]

    async def _project_targets(self, row, retired_sink, deferred_sink):
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
            batches += (await conn.execute(select(integration_batches).where(
                integration_batches.c.project_id == project_id,
                integration_batches.c.repository_id == repository_id,
                integration_batches.c.fixer_queue["status"].astext == "deferred",
            ))).mappings().all()
            for batch in batches:
                queue = batch["fixer_queue"] or {}
                if queue.get("status") == "deferred":
                    key = (project_id, repository_id, batch["target_ref"])
                    deferred_sink[key] = min(
                        deferred_sink.get(key, float("inf")), queue["revisit_at"],
                    )
            completed = (await conn.execute(select(tasks.c.id).where(
                tasks.c.project_id == project_id,
                or_(tasks.c.repo_id == repository_id, tasks.c.repo_id.is_(None)),
                tasks.c.status == "COMPLETED",
            ))).scalars().all()
            closed_epics = await _epic_branches_on(conn, completed)
            independent = set(closed_epics) - set(pending)
            completed_aggregates = set((await conn.execute(select(
                task_completion_records.c.task_id,
            ).where(task_completion_records.c.task_id.in_(independent)))).scalars())
            legacy_containers = independent - completed_aggregates
            children: dict[str, set[str]] = {}
            for child_id, parent_id in (await conn.execute(select(
                tasks.c.id, tasks.c.parent_task_id,
            ).where(tasks.c.parent_task_id.in_(legacy_containers)))).all():
                children.setdefault(parent_id, set()).add(child_id)
        delivered = set()
        contained_epics = set()
        observed = None
        if pending or closed_epics:
            try:
                async with asyncio.timeout(self.probe_timeout_seconds):
                    observed = await self.snapshot(self.db, root)
                    delivered = await project_delivered(
                        self.db, sorted(set(pending) | set(closed_epics) |
                                        {child for ids in children.values() for child in ids}),
                        project_id=project_id, repository_id=repository_id,
                        target_ref=default, snapshot=observed,
                    )
                    if observed is not None and not observed.error and observed.target_oid:
                        # Legacy containers can lack their own completion even
                        # though every child and their private tip reached the
                        # root. Preserve that proof; unknown child delivery or
                        # ancestry must retain the independent aggregate visit.
                        provenance = GitProvenance(
                            observed.observation.git, observed.observation.store,
                            repository_url=observed.observation.repository_url,
                        )
                        for task_id in sorted(legacy_containers):
                            if not children.get(task_id, set()) <= delivered:
                                continue
                            head = observed.for_target(_branch(closed_epics[task_id])).target_oid
                            if head is None:
                                continue
                            facts = observed.truth._pair(observed.observation, head)
                            if facts.ancestor is None:
                                facts.ancestor = await provenance.ancestor(head, observed.target_oid)
                            if facts.ancestor:
                                contained_epics.add(task_id)
            except (TimeoutError, GitError, OSError):
                logger.warning("integration root discovery probe unavailable for %s", root.key)
        retired = set()
        closed_refs = {_branch(branch) for branch in closed_epics.values()}
        if closed_refs and observed is not None:
            if (observed.error in {None, "target_ref_missing"}
                    and not observed.observation.acquisition_error):
                retired = {ref for ref in closed_refs if
                           observed.for_target(ref).error == "target_ref_missing"}
            else:
                # Ref retirement needs no object bodies. On acquisition failure,
                # a bounded independent remote read can still establish absence.
                try:
                    async with asyncio.timeout(self.probe_timeout_seconds):
                        remote = await observed.observation.git.als_remote_refs(
                            observed.observation.store, sorted(closed_refs),
                            repository_url=observed.observation.repository_url,
                        )
                    retired = {ref for ref, result in remote.items()
                               if result.state is RemoteRefState.ABSENT}
                except (TimeoutError, GitError, OSError):
                    pass
        # A frozen batch still needs its recovery diagnostics, even if its
        # container has since completed or its remote target disappeared.
        retired -= {default, *promotions, *(batch["target_ref"] for batch in batches)}
        retired_sink.update((project_id, repository_id, ref) for ref in retired)
        refs = {default, *(batch["target_ref"] for batch in batches)}
        refs.update(target.target_ref for task_id, target in routed.items()
                    if task_id not in delivered and
                    (target.project_id, target.repository_id) == (project_id, repository_id))
        refs.update(_branch(branch) for task_id, branch in epics.items() if task_id not in delivered)
        # Settlement is implementation bookkeeping, not aggregate verification.
        # A completed root has no parent whose pending children could rediscover
        # its lane. Keep its own visit even after origins have retired; ordinary
        # readiness still refuses missing source identity instead of inventing it.
        refs.update(_branch(branch) for task_id, branch in closed_epics.items()
                    if task_id not in delivered and task_id not in contained_epics)
        refs.update(promotions)
        refs -= retired
        return [root if ref == default else promotions.get(ref) or
                TrainTarget(project_id, repository_id, ref, "epic") for ref in sorted(refs)]


def _conflict_action(refusal, snapshot, *, epic: bool, refresh=None):
    """Name who resolves a root PR that conflicts with the default branch."""
    if refusal.get("code") != "pr_conflicting":
        return refusal
    action = "the train tries a mechanical merge, then reopens the original task for real conflicts"
    detail = {}
    if refresh is not None:
        detail["refresh"] = refresh
        outcome = refresh.get("outcome")
        action = {
            "reopened": "the original task was reopened on its branch; run targeted tests only",
            "refreshed": "the bot merged and pushed the default branch; reobserve the new source",
            "already_handled": "this head/default pair was already handled; awaiting a new head",
            "stale": "source or default tip changed; reobserve before refreshing",
        }.get(outcome, "mechanical refresh unavailable; the train will retry")
        if not refresh.get("success"):
            detail["code"] = "refresh_unavailable"
            detail.update({key: refresh[key] for key in ("retry_at", "retry_seconds")
                           if key in refresh})
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
                    await execute_task_status_mutation(conn, update(tasks).where(tasks.c.id == old_id)
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
                 pr_gate: Callable | None = None, cleanup=None, refresh_conflict=None):
        self.db, self.limit, self.clock = db, limit, clock
        self._stack_scan = {}
        self.pr_gate = pr_gate
        self.repaired_source_gate = getattr(pr_gate, "admit_repaired_source", None)
        self.cleanup = cleanup
        self.refresh_conflict = refresh_conflict
        self._refresh_snapshots = {}
        # Scheduling hints only: every continuation reloads rows and Git proof.
        # Advance before awaiting a candidate so cancellation cannot pin a prefix.
        self._selection_after: dict[tuple[str, str, str], str] = {}
        self._selection_clock = time.monotonic
        # Refusal diagnostics only, never input to admission or publication.
        # Frozen visits retain explicitly dated observations without rescanning.
        self._frontier_observations: dict[tuple[str, str, str], tuple[dict, ...]] = {}
        self.conflict_handler = None
        self._refresh_backoff = {}

    async def refresh_pair_conflicts(self, target):
        """Retry the durable source-refresh request, never a batch fixer."""
        if self.refresh_conflict is None:
            return
        table = integration_batches
        async with self.db._engine.connect() as conn:
            records = (await conn.execute(select(table.c.id, table.c.ejection_record).where(
                table.c.project_id == target.project_id,
                table.c.repository_id == target.repository_id,
                table.c.target_ref == target.target_ref,
                table.c.intent == "aborted",
                table.c.ejection_record["pair_conflict"]["refresh_status"].astext == "pending",
            ))).all()
        for batch_id_, record in records:
            try:
                result = await self.refresh_conflict(batch_id_, record)
            except Exception:
                logger.warning("Source refresh for conflict batch %s will retry", batch_id_,
                               exc_info=True)
                continue
            if not result.get("success"):
                continue
            conflict = {**record["pair_conflict"], "refresh_status": "requested",
                        "refresh_result": result}
            async with self.db.immediate() as conn:
                await conn.execute(update(table).where(
                    table.c.id == batch_id_, table.c.ejection_record == record,
                ).values(ejection_record={**record, "pair_conflict": conflict}))

    async def drop_conflict(self, target, batch, members, observation, snapshot, service):
        """Replace a root batch with its dependency-closed healthy remainder.

        Rebuild from the target, never the partial candidate: an earlier member
        may itself depend on the rejected member. Refresh/promotion and existing
        repair writers retain their own publication and recovery semantics.
        """
        if (target.kind not in {"root", "development"} or batch.epic_sync or batch.epic_refresh
                or batch.repair_attempt_count):
            return None
        detail = observation.detail or {}
        # A regenerator's unsafe output is a batch/configuration fault, not
        # evidence that this member conflicts. Keep its existing repair path.
        if detail.get("reason") not in {"merge_conflict", "alembic_head_collision"}:
            return None
        task_id = detail.get("member")
        frozen = {m.task_id: m for m in members}
        if task_id not in frozen:
            return None
        row = await self.db.get_integration_batch(batch.id)
        if row["fixer_state"] or row["fixer_queue"]:
            return None

        async def dependency_edges():
            async with self.db._engine.connect() as conn:
                return set((await conn.execute(select(
                    task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id,
                ).join(tasks, tasks.c.id == task_dependencies.c.task_id).where(
                    tasks.c.project_id == batch.project_id,
                    task_dependencies.c.dep_type == "blocks",
                ))).all())

        edges = await dependency_edges()
        repo = await service.gitops.repository(batch)
        removed = {task_id}
        checked = set()
        while True:
            # Include paths through a prerequisite outside this batch too.
            while True:
                closure = removed | {tid for tid, need in edges if need in removed}
                if closure == removed:
                    break
                removed = closure
            unchecked = (removed & frozen.keys()) - checked
            if not unchecked:
                break
            for tid in sorted(unchecked):
                for member in members:
                    if member.task_id not in removed and await service.gitops.is_ancestor(
                        repo, frozen[tid].source_sha, member.source_sha,
                    ):
                        removed.add(member.task_id)
            checked |= unchecked
        removed &= frozen.keys()
        remaining = tuple(replace(m, order=i) for i, m in enumerate(
            m for m in members if m.task_id not in removed))
        replacement = None
        trees = {}
        if remaining:
            digest = hashlib.sha256(repr((batch.id, task_id, remaining,
                                         snapshot.target_oid)).encode()).hexdigest()
            replacement = replace(batch, id="train-pair-" + digest[:32], created_at=self.clock())
            requests = await load_delivery_requests(
                self.db, [m.task_id for m in remaining], repository_id=batch.repository_id,
                target_ref=batch.target_ref, reduced=True,
            )
            remaining, trees = await service.freeze_inputs(
                replacement, remaining, requests=requests, snapshot=snapshot)

        async def authorize():
            return (await service._authorized(batch, members)
                    and await dependency_edges() == edges
                    and await service.gitops.remote(repo, batch.target_ref) == snapshot.target_oid
                    and await service.gitops.remote(repo, candidate_ref(batch.id))
                    == observation.candidate_sha)

        reason = "pair_conflict:" + task_id
        source_requests = await load_delivery_requests(
            self.db, [task_id], repository_id=batch.repository_id,
            target_ref=batch.target_ref, reduced=True,
        )
        conflict = {"task_id": task_id, "target_sha": snapshot.target_oid,
                    "sources": {tid: frozen[tid].source_sha for tid in sorted(removed)},
                    "completion_id": source_requests[task_id].completion_id,
                    "files": detail.get("files", []), "refresh_status": "pending"}
        await service.store.eject(
            batch, task_id, replacement, remaining, trees=trees, authorize=authorize,
            dry_run=False, operator_id=TRAIN_HOLDER, reason=reason,
            dependent_ids=removed - {task_id}, pair_conflict=conflict,
        )
        await self.refresh_pair_conflicts(target)
        return BatchSelection(batch=replacement, members=remaining, blockers=({
            "code": reason, "task_id": task_id, "batch_id": batch.id,
            "ejected_members": sorted(removed), "target_sha": snapshot.target_oid,
            "replacement_batch_id": replacement.id if replacement else None,
        },))

    async def _refresh_conflicting_prs(self, target, snapshot, blockers, service) -> None:
        """Let CommandHandler merge first and dispatch only a real source conflict."""
        if self.conflict_handler is None:
            return
        from src.integration.root_pr_refresh import RootPRConflict

        for index, blocker in enumerate(blockers):
            if blocker.get("code") not in {"pr_conflicting", "root_pr_refresh_pending"}:
                continue
            task_id = blocker["task_id"]
            pair = (task_id, blocker["source_sha"], blocker.get("default_sha", snapshot.target_oid))
            previous = self._refresh_backoff.get(pair)
            if previous and self.clock() < previous["retry_at"]:
                blockers[index] = _conflict_action({**blocker, "code": "pr_conflicting"}, snapshot,
                    epic=blocker.get("epic", False), refresh=previous)
                continue
            async with self.db._engine.connect() as conn:
                request = (await load_delivery_requests(self.db, [task_id],
                    repository_id=target.repository_id, target_ref=target.target_ref,
                    conn=conn, reduced=True)).get(task_id)
                project = (await conn.execute(select(projects).where(
                    projects.c.id == target.project_id))).mappings().one()
                base = await conn.scalar(select(task_branch_origins.c.base_sha).where(
                    task_branch_origins.c.task_id == task_id,
                    task_branch_origins.c.repository_id == target.repository_id,
                    task_branch_origins.c.retired_at.is_(None),
                ).limit(1))
            if request is None or not base or not blocker.get("pr_url"):
                continue
            try:
                result = await self.conflict_handler(RootPRConflict(
                    request, blocker["source_sha"], base,
                    blocker.get("default_sha", snapshot.target_oid), blocker["pr_url"],
                    project["hierarchical_integration_generation"],
                    project["hierarchical_integration_policy"] or {},
                ), service.gitops, snapshot)
            except Exception as exc:
                logger.warning("Mechanical root refresh unavailable for %s", task_id, exc_info=True)
                result = {"success": False, "outcome": "unavailable", "reason": type(exc).__name__}
            if not result.get("success"):
                delay = min((previous or {}).get("retry_seconds", 30) * 2, 600)
                result = {**result, "retry_at": self.clock() + delay, "retry_seconds": delay}
                self._refresh_backoff[pair] = result
            else:
                self._refresh_backoff.pop(pair, None)
            blockers[index] = _conflict_action({**blocker, "code": "pr_conflicting"}, snapshot,
                                               epic=blocker.get("epic", False), refresh=result)

    async def prepare_speculative(self, target, service, parent, base, snapshot):
        frozen = await service.store.members(parent.id)
        ids = {member.task_id for member in frozen}
        pending = await self.pending(target, snapshot, include_ids=ids)
        if pending is None:
            return None
        members, requests, dependencies = pending
        members = tuple(member for member in members if member.task_id not in ids)
        if not members:
            return None
        dependencies = {key: tuple(dep for dep in value if dep not in ids)
                        for key, value in dependencies.items() if key not in ids}
        batch = Batch(await self.next_batch_id(target, members),
                      target.project_id, target.repository_id, target.target_ref,
                      created_at=self.clock())
        members, _ = await service.freeze_inputs(
            batch, members, requests=requests, snapshot=snapshot,
            dependencies=dependencies)
        return BatchSelection(batch, members)

    async def queued_members(self, target, snapshot, members):
        """Reconstruct the next queue from durable completions, never frozen writes.

        Include the in-flight sources while checking closure: their dependents
        can queue now but cannot freeze until those sources settle on the tip.
        Every departure revalidates the complete frontier on its new Git view.
        """
        if target.kind != "root":
            return ()
        frozen_ids = {member.task_id for member in members}
        pending = await self.pending(
            target, snapshot, include_ids=frozen_ids,
            selection_deadline=self._selection_clock() + SELECTION_WORK_SECONDS,
        )
        return (tuple(member for member in pending[0] if member.task_id not in frozen_ids)
                if pending else ())

    async def open_batch(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, service: BatchService, *,
        seal_now: bool = False,
    ) -> BatchSelection:
        with selection_stage("reconcile_aborted"):
            await service.store.reconcile_aborted(target=target)
        # One fetched observation per serialized visit. Eligibility is called
        # again inside publication locks and must only read these local facts.
        self._refresh_snapshots[target.key] = snapshot
        await _finish_on_cancel(self.refresh_pair_conflicts(target))
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db, clock=self.clock)
        admission_blockers = []
        delivered_ids = set()
        current = await self.current(target)
        if current is None:
            from src.integration.fixer_batches import FixerBatches

            current = await FixerBatches(self.db, clock=self.clock).resume(target, snapshot, service)
        if current is None:
            from src.integration.repository_incidents import admission_holds

            incidents = await admission_holds(self.db, target.project_id)
            if incidents:
                return BatchSelection(blockers=({
                    "code": "retained_repository_recovery", "ref": target.project_id,
                    "detail": "New project batches wait for retained Git cache recovery",
                    "incident_ids": [item["id"] for item in incidents],
                },))
        frozen_members = await service.store.members(current.id) if current is not None else ()
        if current is not None and current.repair_attempt_count:
            from src.integration.repair import OrdinaryRepairService

            # Selection and rebuilding may refuse before _visit_batch reaches
            # recovery. A stopped repair must retain a path back to the frontier.
            await OrdinaryRepairService(self.db).recover_published_reservation(
                current, frozen_members, snapshot, service,
            )
        if (target.kind == "epic" and current is not None and frozen_members
                and current.intent == "open" and not (current.epic_sync or current.epic_refresh)
                and await _finish_on_cancel(self._settle_project_delivered(
                    target, snapshot, service, current, frozen_members))):
            return BatchSelection(detail={
                "reason": "batch_inputs_delivered_to_project", "batch_id": current.id,
            })
        if (current is not None and frozen_members
                and not (current.epic_sync or current.epic_refresh)
                and target.kind != "promotion"
                and not snapshot.error and snapshot.target_oid
                and all([await snapshot.contains_source(m.task_id, m.source_sha, m.base_sha)
                         is True for m in frozen_members])):
            # Historical delivery precedes fresh source admission: a PR may
            # now be closed, its branch removed or its check policy changed.
            # The service still checks holds and the exact frozen membership.
            return BatchSelection(current, frozen_members, existing=True)
        # A frozen batch already owns this target. New candidates must not delay
        # its checks or its existing repair; publication revalidates its members.
        candidates = ([member.task_id for member in frozen_members] if current is not None else
                      await self._candidate_ids(target, snapshot, blockers=admission_blockers,
                                                delivered_ids=delivered_ids, continuation=True)
                      if not snapshot.error and snapshot.target_oid else [])
        async with self.db._engine.connect() as conn:
            stacked_ids = (await conn.execute(select(task_branch_origins.c.task_id).where(
                    task_branch_origins.c.task_id.in_(candidates),
                    task_branch_origins.c.repository_id == target.repository_id,
                    task_branch_origins.c.retired_at.is_(None),
                    task_branch_origins.c.stack_snapshot.is_not(None)))).scalars().all()
        refresh_deadline = self._selection_clock() + SELECTION_WORK_SECONDS
        for index, task_id in enumerate(sorted(stacked_ids, key=candidates.index)):
            if index and current is None and self._selection_clock() >= refresh_deadline:
                selection_count("stack_refresh_deferred", len(stacked_ids) - index)
                break  # Unrefreshed stacks still face current/source containment guards.
            if current is None:
                self._selection_after[target.key] = task_id
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
        if current is not None and current.repair_attempt_count:
            from src.integration.repair import OrdinaryRepairService, ordinary_repair_id

            await OrdinaryRepairService(self.db).resume_completed(
                ordinary_repair_id(current.id, current.repair_attempt_count),
                batch_id=current.id, attempt=current.repair_attempt_count)
        if current is not None and target.kind != "promotion":
            policy_blocker = await self._supersede_removed_checks(
                target, snapshot, service, current, frozen_members,
            )
            if policy_blocker:
                return BatchSelection(
                    current if current.intent == "paused" else None,
                    frozen_members if current.intent == "paused" else (),
                    (policy_blocker,), existing=True,
                )
        pending = None
        evaluated_ids: set[str] = set()
        if not snapshot.error and snapshot.target_oid:
            if candidates:
                pending = await self.pending(target, snapshot, blockers=blockers,
                                             gate_pr=current is None,
                                             include_ids={member.task_id for member in frozen_members},
                                             candidate_ids=candidates, delivered_ids=delivered_ids,
                                             evaluated_ids=evaluated_ids,
                                             selection_deadline=(self._selection_clock() + SELECTION_WORK_SECONDS
                                                                 if current is None else None))
            if current is None:
                await _finish_on_cancel(self._refresh_conflicting_prs(
                    target, snapshot, blockers, service))
            if current is None:
                self._frontier_observations[target.key] = tuple({
                    **blocker, "observation": "last_selection", "observed_at": self.clock(),
                    "observed_target_sha": snapshot.target_oid,
                } for blocker in blockers if blocker.get("task_id"))
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
                                  "complete frozen-source proof is required for automatic settlement",
                        "task_ids": sorted(delivered),
                    })
                    return BatchSelection(blockers=tuple(blockers))
            # These previous refusals explain sources outside the frozen batch.
            # Append only after its fresh member guards: diagnostics confer no
            # authority and cannot withhold or release the current batch.
            reported = {blocker.get("task_id") for blocker in blockers}
            member_ids = {member.task_id for member in members}
            blockers.extend(blocker for blocker in self._frontier_observations.get(target.key, ())
                            if blocker["task_id"] not in reported | member_ids)
            return BatchSelection(current, members, tuple(blockers), existing=True)
        if pending is None:
            if target.kind == "root" and not snapshot.error and snapshot.target_oid:
                await self._clear_admissions(target,
                    evaluated_ids=evaluated_ids if blockers else None)
            return BatchSelection(blockers=tuple(blockers))
        from src.integration.train_policy import load_train_policy

        view = await load_train_policy(self.db, target.project_id)
        if not view.enabled or (view.policy and view.policy.action("idle") == "wait"):
            return BatchSelection(detail={"reason": view.reason or "train_policy_wait",
                "retry_at": self.clock() + (view.policy.wait_seconds if view.policy else 30)})
        members, requests, dependencies = pending
        now = self.clock()
        # A live batch above owns the lane until settlement. With no live batch,
        # freeze the ready frontier immediately. Old timing hints and policy
        # snapshots are parseable but never defer current dispatch.
        async with self.db._engine.connect() as conn:
            carried = await released_repair_attempts_on(conn, target, members)
        batch = Batch(id=await self.next_batch_id(target, members), project_id=target.project_id,
                      repository_id=target.repository_id, target_ref=target.target_ref,
                      repair_attempt_count=carried, created_at=now)
        with selection_stage("freeze", items=len(members)):
            frozen = await service.freeze(batch, members, requests=requests, snapshot=snapshot,
                                          dependencies=dependencies)
        if target.kind == "root":
            await self._clear_admissions(target)
        return BatchSelection(frozen, await service.store.members(frozen.id), tuple(blockers))

    async def _settle_project_delivered(self, target, snapshot, service, batch, members) -> bool:
        """Release an obsolete epic batch using its frozen inputs, even without its ref.

        This observes delivery to the project, not publication to the epic. Do
        not emit epic source-settled/promotion policy events or infer delivery
        of a newer completion from this historical batch's inputs.
        """
        from src.operator_decisions import history_on, related_on

        observed = snapshot.observation
        if (observed.project_id, observed.repository_id, observed.target_ref) != target.key:
            return False
        async with self.db._engine.connect() as conn:
            binding = (await conn.execute(select(repos.c.default_branch, repos.c.url)
                .join(projects, projects.c.integration_repository_id == repos.c.id)
                .where(projects.c.id == batch.project_id,
                       repos.c.id == batch.repository_id))).first()
        if binding is None:
            return False
        root_ref = _branch(binding.default_branch)
        if root_ref == batch.target_ref:
            return False
        root = snapshot.for_target(root_ref)
        if root.error or not root.target_oid or root.observation.repository_url != binding.url:
            return False
        for member in members:
            if await root.contains_source(
                member.task_id, member.source_sha, member.base_sha,
            ) is not True:
                return False
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, batch.project_id)
            project = (await conn.execute(select(projects).where(
                projects.c.id == batch.project_id).with_for_update())).mappings().one()
            repository = (await conn.execute(select(repos).where(
                repos.c.id == batch.repository_id).with_for_update())).mappings().one()
            if (project["integration_repository_id"] != batch.repository_id
                    or project["status"] != "ACTIVE"
                    or project["hierarchical_integration_mode"] not in TRAIN_MODES
                    or _branch(repository["default_branch"]) != root_ref
                    or repository["url"] != binding.url):
                return False
            await service.store._target_lock(conn, batch)
            row = (await conn.execute(select(integration_batches).where(
                integration_batches.c.id == batch.id).with_for_update())).mappings().one()
            frozen = tuple(BatchMember(r.task_id, r.source_sha, r.source_base_sha, r.ordinal)
                for r in (await conn.execute(select(integration_batch_members).where(
                    integration_batch_members.c.batch_id == batch.id,
                ).order_by(integration_batch_members.c.ordinal))).all())
            if (Batch.from_row(row) != batch or row["intent"] != "open"
                    or row["lifecycle"] == "promoted" or row["trigger"] == "promotion"
                    or frozen != members):
                return False
            decisions = await history_on(conn, batch.project_id,
                                         await related_on(conn, "batch", batch.id))
            if any(decision["active"] for decision in decisions) or not await root.is_fresh():
                return False
            candidate = root.observation.source_heads.get(
                "refs/remotes/origin/" + candidate_ref(batch.id).removeprefix("refs/heads/"))
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id,
            ).values(lifecycle="promoted", final_main_sha=root.target_oid,
                     tested_candidate_sha=candidate, updated_at=self.clock()))
            from src.integration.repair import OrdinaryRepairService

            await OrdinaryRepairService(self.db)._settle_green_on(conn, batch.id, root.target_oid)
            await self.db.log_event("integration.batch_delivered_to_project",
                project_id=batch.project_id, payload=json.dumps({
                    "batch_id": batch.id, "target_ref": target.target_ref,
                    "delivered_ref": root_ref, "delivered_sha": root.target_oid,
                    "members": [asdict(member) for member in members],
                }), conn=conn)
        await self.cleanup_settled(batch)
        return True

    async def next_batch_id(self, target, members):
        """Ejected or released sources may return with the same exact frozen inputs.

        The aborted batch stays immutable history under its own id.
        """
        identity = batch_id(target, members)
        async with self.db._engine.connect() as conn:
            while True:
                intent = await conn.scalar(select(integration_batches.c.intent).where(
                    integration_batches.c.id == identity))
                if intent != "aborted":
                    return identity
                identity = "train-" + hashlib.sha256((identity + ":readmit").encode()).hexdigest()[:32]

    async def current(self, target: TrainTarget) -> Batch | None:
        return next((batch for batch, refreshed in await self._open_batches(target)
                     if refreshed is None), None)

    async def reassemble_preexisting(
        self, target, snapshot, service, batch, frozen, *, names, source_checks,
    ):
        """Admit exact green baseline fixes without losing the batch they unblock."""
        from src.integration.checks import Conclusion
        from src.integration.train_controls import TrainControlError, TrainControls
        from src.operator_decisions import OperatorDecisions

        if (batch.intent != "open" or batch.epic_sync or batch.epic_refresh or not names
                or await OperatorDecisions(self.db).holds("batch", batch.id)):
            return None
        # Rotate a bounded frontier even while a frozen batch owns the target.
        ids = await self._candidate_ids(target, snapshot, continuation=True,
                                       include_ids={m.task_id for m in frozen})
        pending = await self.pending(target, snapshot, gate_pr=False, candidate_ids=ids,
                                     include_ids={m.task_id for m in frozen})
        if pending is None:
            return None
        members, requests, dependencies = pending
        originals = {m.task_id: m for m in frozen}
        if any(not any(m.task_id == old.task_id and m.source_sha == old.source_sha
                       and m.base_sha == old.base_sha for m in members) for old in frozen):
            return None
        accepted = dict(originals)
        for member in members:
            if member.task_id in originals:
                continue
            self._selection_after[target.key] = member.task_id
            if self.pr_gate is None or await self.pr_gate(target, member):
                continue
            result = await source_checks(member)
            passed = ({c.name for c in result.checks if c.conclusion is Conclusion.SUCCESS}
                if (result is not None and result.green and result.sha == member.source_sha
                    and result.repository_id == target.repository_id) else set())
            if set(names) <= passed:
                accepted[member.task_id] = member
        # A green source cannot bring an unaccepted prerequisite along with it.
        while True:
            unsafe = {tid for tid in accepted if tid not in originals
                      and set(dependencies.get(tid, ())) - accepted.keys()}
            if not unsafe:
                break
            for tid in unsafe:
                accepted.pop(tid)
        if len(accepted) == len(originals):
            return None
        if any(set(dependencies.get(tid, ())) - accepted.keys() for tid in originals):
            return None
        members = tuple(m for m in members if m.task_id in accepted)
        requests = {tid: requests[tid] for tid in accepted}
        async with self.db._engine.connect() as conn:
            carried = await released_repair_attempts_on(conn, target, members)
        replacement = replace(batch,
            id="train-reassemble-" + hashlib.sha256(repr((batch.id, members)).encode()).hexdigest()[:32],
            repair_attempt_count=max(batch.repair_attempt_count, carried),
            created_at=self.clock())
        members, trees = await service.freeze_inputs(
            replacement, members, requests=requests, snapshot=snapshot, dependencies=dependencies)
        controls = TrainControls(self.db)

        async def authorize(conn, row):
            current = await load_delivery_requests(self.db, list(requests), conn=conn,
                repository_id=batch.repository_id, target_ref=batch.target_ref, reduced=True)
            if current != requests or not await snapshot.is_fresh():
                return False
            for tid in accepted:
                await controls._release_refusal(conn, batch, tid, requests[tid], row["policy_snapshot"])
            return True

        try:
            replaced = await service.store.reassemble(
                batch, replacement, members, trees=trees, authorize=authorize)
        except TrainControlError as exc:
            return {"outcome": "blocked", "blocker": exc.outcome, "reason": str(exc)}
        return {"outcome": "reassembled" if replaced else "changed",
                "replacement_batch_id": replacement.id,
                "added_members": sorted(accepted.keys() - originals.keys()),
                "pre_existing_checks": list(names)}

    async def _supersede_removed_checks(self, target, snapshot, service, batch, members):
        """Release obsolete check pins without rewriting frozen policy or sources."""
        from src.integration.train_policy import load_train_policy
        from src.operator_decisions import OperatorDecisions

        # Live playbook policy applies to this same candidate, without a requeue.
        if (await load_train_policy(self.db, target.project_id)).policy is not None:
            return None
        boundary = "parent" if target.kind == "epic" else "root"
        removed = await service.store.removed_required_checks(batch, boundary)
        if not removed:
            return None
        detail = f"frozen {boundary} checks removed from project policy: {', '.join(removed)}"
        blocker = {"code": "batch_required_checks_removed", "ref": batch.id,
                   "batch_id": batch.id, "boundary": boundary,
                   "removed_checks": list(removed), "released": False, "detail": detail}
        if batch.intent != "open" or await OperatorDecisions(self.db).holds("batch", batch.id):
            return {**blocker, "detail": detail + "; explicit hold retains the batch; "
                    "resume it through the supervisor control to allow replacement"}
        if snapshot.error or not snapshot.target_oid or not members:
            return {**blocker, "detail": detail + "; Git evidence is unavailable"}
        proofs = [await snapshot.contains_source(m.task_id, m.source_sha, m.base_sha)
                  for m in members]
        if any(proof is None for proof in proofs):
            return {**blocker, "detail": detail + "; batch delivery cannot be observed"}
        if all(proofs) and not (batch.epic_sync or batch.epic_refresh):
            # Git inclusion remains authoritative even if the policy changed
            # between publication and the visit which records its settlement.
            return None
        try:
            released = await service.store.supersede(
                batch, members[0].task_id, reason=detail + "; a new batch replaces it",
                boundary=boundary, removed_checks=removed, authorize=snapshot.is_fresh,
            )
        except SupersedeMemberUnavailable as exc:
            return {**blocker, "code": exc.code, "task_id": exc.task_id,
                    "detail": f"{detail}; {exc}; restore the member identity or have a "
                              f"supervisor preview aq integration abort-batch {batch.id}"}
        return {**blocker, "released": released, "detail": detail + (
            "; inputs return to admission for a new batch" if released else
            "; policy, batch or target changed; recheck on the next visit")}

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

    async def _clear_admissions(self, target: TrainTarget, *, evaluated_ids=None) -> None:
        async with self.db.immediate() as conn:
            removal = delete(task_metadata).where(
                task_metadata.c.key == self._admission_key(target),
            )
            if evaluated_ids is not None:
                removal = removal.where(task_metadata.c.task_id.in_(evaluated_ids))
            await conn.execute(removal)

    async def _candidate_ids(self, target: TrainTarget, snapshot: GitTruthSnapshot, *,
                             include_ids: set[str] | None = None, blockers=None,
                             delivered_ids: set[str] | None = None, continuation=False):
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
        requests = await load_delivery_requests(
            self.db, ids, repository_id=target.repository_id, target_ref=target.target_ref,
            reduced=True,
        )
        excluded = {tid for tid, request in requests.items() if request.lifecycle_disposition}
        selection_count("excluded_delivered_by_other_means", len(excluded))
        ids = [tid for tid in ids if tid not in excluded]
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
        if target.kind == "root":
            # The root queue has no member window. Closure must see every ready
            # prerequisite, including sources beyond the old selection cap.
            # Keep work-quantum rotation: unknown/slow facts cannot strand an
            # already eligible prefix. This is no batching delay or size cap.
            if continuation:
                after = self._selection_after.get(target.key, "")
                ids = [tid for tid in ids if tid > after] + [tid for tid in ids if tid <= after]
            return list(dict.fromkeys([*ids, *sorted(include_ids or ())]))
        if continuation:
            # Rotate the full owed frontier before the member cap; otherwise
            # older work outside the newest window could never be attempted.
            after = self._selection_after.get(target.key, "")
            ids = [tid for tid in ids if tid > after] + [tid for tid in ids if tid <= after]
            selection_count("candidate_window_deferred", max(0, len(ids) - self.limit))
            if blockers is not None and len(ids) > self.limit:
                blockers.append({
                    "code": "selection_deferred", "ref": target.target_ref,
                    "detail": "Candidate window capped; remaining sources need a fresh visit",
                    "remaining": len(ids) - self.limit, "after_task_id": after or None,
                })
            return list(dict.fromkeys([*ids[:self.limit], *sorted(include_ids or ())]))
        async with self.db._engine.connect() as conn:
            if len(ids) > self.limit:
                frozen_ids = [task_id for task_id in ids if task_id in (include_ids or ())]
                ids = list((await conn.execute(select(tasks.c.id).where(tasks.c.id.in_(ids))
                           .order_by(tasks.c.updated_at.desc(), tasks.c.id)
                           .limit(self.limit))).scalars().all())
                ids = list(dict.fromkeys([*ids, *frozen_ids]))
        return ids

    async def _local_pr_conflict(self, snapshot, member):
        """Screen exact fetched inputs without running source code or reading GitHub.

        Like gitops, read generated-path attributes from the merged tree. Those
        artifacts are rebuilt during construction; only source conflicts need
        an author refresh. An unavailable probe withholds admission as unknown.
        """
        observed = snapshot.observation
        blocker = {"task_id": member.task_id, "ref": member.task_id,
                   "source_sha": member.source_sha}
        try:
            result = await observed.git.arun_git_result(
                ["--no-replace-objects", "merge-tree", "--write-tree", "--name-only", "-z",
                 snapshot.target_oid, member.source_sha], cwd=observed.store,
            )
            if result.returncode not in {0, 1}:
                raise GitError(result.stderr or "local PR merge inspection failed")
            tree = result.stdout.split("\0", 1)[0]
            if not is_valid_git_oid(tree):
                raise GitError("local PR merge inspection did not produce a tree OID")
            if result.returncode:
                conflicts = set(result.stdout.split("\0\0", 1)[0].split("\0")[1:]) - {""}
                if not conflicts:
                    raise GitError("local PR merge inspection did not name conflicting files")
                generated = await generated_paths(observed.git, Path(observed.store), tree, conflicts)
                if conflicts - generated:
                    return {**blocker, "code": "pr_conflicting", "files": sorted(conflicts - generated),
                            "detail": "Source conflicts with the fetched target"}
        except (GitError, OSError, ValueError) as exc:
            return {**blocker, "code": "unknown", "detail": "Local PR merge inspection unavailable",
                    "reason": str(exc)}
        return None

    async def pending(
        self, target: TrainTarget, snapshot: GitTruthSnapshot, *,
        blockers: list[dict[str, Any]] | None = None, gate_pr: bool = True,
        include_ids: set[str] | None = None,
        candidate_ids: list[str] | None = None,
        delivered_ids: set[str] | None = None, selection_deadline: float | None = None,
        evaluated_ids: set[str] | None = None,
    ):
        """Exact pending inputs; report unknown delivery that prevents batching."""
        from src.integration.github_review_poll import root_pr_target_scope

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
            pr_urls = dict((await conn.execute(select(tasks.c.id, tasks.c.pr_url).where(
                tasks.c.id.in_(ids),
            ))).all())
            from src.integration.root_pr_refresh import REFRESH_KEY

            refreshes = {tid: json.loads(value) for tid, value in (await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.value).where(
                    task_metadata.c.task_id.in_(ids), task_metadata.c.key == REFRESH_KEY,
                ))).all()} if gate_pr and target.kind == "root" else {}
            # The latest aborted batch still withholding each exact source.
            # Only its ejection or an explicit operator release frees it.
            withheld = {(row.task_id, row.source_sha): row for row in (await conn.execute(
                withheld_sources_query(target).where(
                    integration_batch_members.c.task_id.in_(ids))
                .order_by(integration_batches.c.created_at, integration_batches.c.id)
            )).all()}
            pair_conflicts = {(row.task_id, row.source_sha): row for row in (await conn.execute(
                pair_conflict_sources_query(target, snapshot.target_oid).where(
                    integration_batch_members.c.task_id.in_(ids))
            )).all()}
            repairs = set((await conn.execute(select(task_metadata.c.task_id).where(
                task_metadata.c.task_id.in_(ids), task_metadata.c.key == "stack_repair_for",
            ))).scalars())
            bases: dict[str, str] = {}
            for task_id, base, _ in (await conn.execute(
                select(task_branch_origins.c.task_id, task_branch_origins.c.base_sha,
                       task_branch_origins.c.creation_generation)
                .where(task_branch_origins.c.task_id.in_(ids),
                       task_branch_origins.c.repository_id == target.repository_id)
                .order_by(task_branch_origins.c.retired_at.is_(None),
                          task_branch_origins.c.creation_generation)
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
        retired = {tid for tid, request in requests.items() if request.lifecycle_disposition}
        withheld_ids: set[str] = set()
        refused_sources = {}
        paired_sources = {}
        delivered: set[str] = set()
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db)
        selection_count("candidates_eligible", len(ids))
        for index, task_id in enumerate(ids):
            if (index and selection_deadline is not None
                    and self._selection_clock() >= selection_deadline):
                selection_count("candidate_work_deferred", len(ids) - index)
                if blockers is not None:
                    blockers.append({
                        "code": "selection_deferred", "ref": target.target_ref,
                        "detail": "Selection work quantum spent; remaining sources need a fresh visit",
                        "remaining": len(ids) - index,
                        "after_task_id": self._selection_after.get(target.key),
                    })
                break
            candidate_started = self._selection_clock()
            selection_count("candidates_evaluated")
            if selection_deadline is not None:
                self._selection_after[target.key] = task_id
            if evaluated_ids is not None:
                evaluated_ids.add(task_id)
            request, base = requests.get(task_id), bases.get(task_id)
            if request is not None and request.lifecycle_disposition is not None:
                # A reused candidate window must recheck this decision too.
                selection_count("excluded_delivered_by_other_means")
                continue
            if task_id in repairs:
                continue
            if request is None or not is_valid_git_oid(base or ""):
                if blockers is not None:
                    blockers.append({
                        "code": "source_identity_unavailable", "ref": task_id, "task_id": task_id,
                        "detail": "Completed source needs a current delivery request and exact origin base",
                    })
                continue
            prepared = next((pair for pair in refreshes.get(task_id, {}).get("pairs", {}).values()
                if pair["outcome"] == "prepared"
                and pair["source_sha"] == request.reported_source
                and pair["source_completion_id"] == request.completion_id), None)
            if prepared is not None:
                if blockers is not None:
                    blockers.append({"code": "root_pr_refresh_pending", "ref": task_id,
                        "task_id": task_id, "source_sha": prepared["source_sha"],
                        "default_sha": prepared["default_sha"], "pr_url": pr_urls.get(task_id),
                        "epic": task_id in epics, "detail": "Finish the prepared bot refresh"})
                continue
            with selection_stage("target_delivery", items=1):
                evidence = await snapshot.strict_is_delivered(request, source_base=base)
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
                    "code": evidence.reason, "ref": task_id, "task_id": task_id, "detail": detail,
                    "repository_id": target.repository_id, "target_ref": target.target_ref,
                })
            source = evidence.source_oid
            if (evidence.state is not DeliveryState.PENDING
                    or evidence.reason != "source_not_contained"
                    or not is_valid_git_oid(source or "")):
                continue
            if target.kind == "epic":
                from src.integration.obsolete_children import obsolete_child_blocker

                obsolete = await obsolete_child_blocker(
                    snapshot.observation, task_id=task_id, source_base=base, source_oid=source,
                )
                if obsolete is not None:
                    if blockers is not None:
                        blockers.append(obsolete)
                    continue
            if (task_id, source) in withheld:
                withheld_ids.add(task_id)
                if blockers is not None:
                    blockers.append(withheld_source_blocker(
                        target, task_id, source, withheld[task_id, source]))
                continue
            if (task_id, source) in pair_conflicts:
                conflict = pair_conflicts[task_id, source]
                withheld_ids.add(task_id)
                if blockers is not None:
                    blockers.append({"code": conflict.human_abort_reason,
                        "task_id": task_id, "ref": task_id, "source_sha": source,
                        "target_sha": snapshot.target_oid, "batch_id": conflict.batch_id,
                        "detail": "Build conflict; source or target head must change before retry"})
                continue
            with selection_stage("stack_source", items=1):
                stack_valid = (await stacks.current(task_id, source_sha=source)
                               and await stacks.source_contains_stack(task_id, source, snapshot))
            if not stack_valid:
                if blockers is not None:
                    blockers.append({
                        "code": "stack_source_changed", "ref": task_id, "task_id": task_id,
                        "detail": "Completion source does not match or contain its prerequisite stack",
                    })
                continue
            member = BatchMember(task_id, source, base)
            if gate_pr and target.kind == "root":
                with selection_stage("local_pr_merge", items=1):
                    refusal = await self._local_pr_conflict(snapshot, member)
                if refusal:
                    refusal["pr_url"] = pr_urls.get(task_id)
                else:
                    with (selection_stage("pr_admission", items=1),
                          root_pr_target_scope(target, snapshot.target_oid)):
                        refusal = (await self.pr_gate(target, member) if self.pr_gate else {
                            "code": "unknown", "ref": task_id, "task_id": task_id,
                            "detail": "root PR admission observer is unavailable",
                        })
                if refusal:
                    if selection_deadline is not None and refusal["code"] in _REFUNDED_PR_REFUSALS:
                        selection_deadline += self._selection_clock() - candidate_started
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
                    with root_pr_target_scope(target, snapshot.target_oid):
                        repaired_refusal = await admit_repaired(target, member)
                    if repaired_refusal is None:
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
            if request.lifecycle_disposition:
                retired.add(task_id)
                continue
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
                if need not in needs and need not in delivered and need not in retired:
                    needs.add(need)
                    remaining.extend(direct_edges.get(need, ()))
            admission_edges[task_id] = needs
        blocked = (set(ids) | required) - members.keys() - delivered - retired
        changed = True
        while changed:
            changed = False
            for task_id in list(members):
                missing = admission_edges.get(task_id, set()) & blocked
                if (missing
                        or (task_id in paired_sources
                            and not paired_sources[task_id] & members.keys())):
                    if missing and blockers is not None:
                        blockers.append({
                            "code": "prerequisite_pending", "ref": task_id, "task_id": task_id,
                            "detail": "Source waits for undelivered prerequisites outside this batch",
                            "prerequisite_task_ids": sorted(missing),
                        })
                    if task_id in paired_sources and blockers is not None:
                        refusal = refused_sources[task_id][1]
                        if refusal not in blockers:
                            blockers.append(refusal)
                    waits_on = admission_edges.get(task_id, set()) & withheld_ids
                    if waits_on and blockers is not None:
                        prerequisite = min(waits_on)
                        blockers.append({
                            "code": "prerequisite_withheld", "ref": task_id, "task_id": task_id,
                            "prerequisite_task_id": prerequisite,
                            "detail": f"task {task_id} waits on prerequisite {prerequisite}, "
                                      "whose exact source an aborted batch withholds",
                            "repository_id": target.repository_id,
                            "target_ref": target.target_ref,
                        })
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
                # Repair filings may share the epic ref. Recheck holds against
                # the frozen epic identity rather than rediscovering it by ref.
                graph = await EpicGraphReader(self.db, policy_on=epic_policy_on,
                    source_base_on=epic_source_base_on).read_on(conn, row["id"]) if row else None
            if not (row and active == "ACTIVE" and row["project_id"] == batch.project_id
                    and row["repo_id"] == batch.repository_id
                    and _branch(row["branch_name"]) == batch.target_ref):
                return False
            if graph.project_holds or any(node.required and node.holds for node in graph.nodes):
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
            recorded_origins = set((await conn.scalars(select(task_branch_origins.c.task_id).where(
                task_branch_origins.c.task_id.in_(ids),
                task_branch_origins.c.retired_at.is_(None),
            ))).all())
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
        if any(request.lifecycle_disposition for request in requests.values()):
            return False
        from src.integration.stacked_branches import StackedBranches

        stacks = StackedBranches(self.db)
        snapshot = self._refresh_snapshots.get(
            (batch.project_id, batch.repository_id, batch.target_ref))
        for member in members:
            if (member.task_id not in recorded_origins
                    and await stacks.completed_source_matches(
                        member.task_id, member.source_sha, snapshot)):
                live.add(member.task_id)
            if not await stacks.current(member.task_id, source_sha=member.source_sha,
                                        snapshot=snapshot, included_sources={
                                            item.task_id: item.source_sha for item in members}):
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
                        source=sources[task_id], snapshot=self._refresh_snapshots.get(
                            (batch.project_id, batch.repository_id, batch.target_ref))):
                    return False
        return True

    async def closed_epic_graph(self, target):
        """Current closed-container identity, including archived/retired children."""
        async with self.db._engine.connect() as conn:
            ids = list(await _epic_branches_on(conn, target=target))
            if len(ids) != 1:
                return None
            graph = await EpicGraphReader(self.db, policy_on=epic_policy_on,
                source_base_on=epic_source_base_on).read_on(conn, ids[0])
        if (not graph.node(graph.epic_id).container
                or graph.repository_id != target.repository_id or graph.project_holds
                or any(node.required and (node.holds
                       or node.task_id != graph.node(graph.epic_id).parent_id
                       and node.request.task_status != "COMPLETED")
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
            proof = await snapshot.strict_is_delivered(request, source_base=child.source_base)
            if proof.state is DeliveryState.NOT_APPLICABLE:
                continue
            if proof.state is not DeliveryState.CONTAINED or not is_valid_git_oid(proof.source_oid):
                return None
            if child.container and proof.source_oid != snapshot.for_target(child.branch_ref).target_oid:
                return None
            members.append(BatchMember(child.task_id, proof.source_oid,
                                       child.source_base or proof.source_oid))
            requests[child.task_id] = request
        return members, requests

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
            graph = await EpicGraphReader(self.db, policy_on=epic_policy_on,
                source_base_on=epic_source_base_on).read_on(conn, task_id)
            notes = json.loads(record["notes"])
            if source is not None and json.loads(record["commits"]) != [source]:
                return False
            if refresh := notes.get("root_pr_refresh"):
                # The bot merge inherits the exact collected epic and its
                # unchanged child graph. Its new combined tree owes batch-tip
                # CI, rather than another pre-admission full check/review.
                return await self._epic_current_on(conn, task_id, refresh["completion_id"],
                                                   source=refresh["source_sha"])
            if snapshot is None and notes["graph_sha256"] != epic_graph_digest(graph):
                return False
            if snapshot is not None:
                # Saved graph hashes include disposable origin/review projections.
                # Fresh exact-source readiness proves the actual graph below; a
                # stale hash cannot override that proof or conceal a real hold.
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

                readiness = await EpicReadinessEvaluator(self.db, EpicGraphReader(self.db,
                    policy_on=epic_policy_on, source_base_on=epic_source_base_on,
                ), TreeReviews(self.db, allow_missing=True), checks=cached_checks).evaluate(graph, snapshot)
                if not readiness.ready or readiness.head_sha != source:
                    return False
                for subject_id, _, _ in epic_heads(readiness):
                    if not await snapshot.for_target(graph.node(subject_id).branch_ref
                                                      ).observation.is_fresh():
                        return False
                return True
            for subject_id, head, tree in notes["heads"]:
                policy = graph.node(subject_id).policy
                if policy.checks_required and policy.check_names:
                    green = set((await conn.execute(select(
                        integration_check_evidence.c.check_name,
                    ).where(integration_check_evidence.c.repository_id == graph.repository_id,
                        integration_check_evidence.c.sha == head,
                        integration_check_evidence.c.producer_id == policy.check_trust,
                        integration_check_evidence.c.required_check_version == policy.check_version,
                        integration_check_evidence.c.conclusion == "success"))).scalars())
                    if not set(policy.check_names) <= green:
                        return False
                review = await TreeReviews(self.db, allow_missing=True).verdict_on(conn, ReviewSubject(
                    graph.project_id, graph.repository_id, subject_id, tree), policy.reviews)
                if not review.satisfied:
                    return False
        except (ValueError, KeyError, TypeError):
            return False
        return True

    async def record_candidate(self, batch: Batch, observation: BatchObservation) -> None:
        """Bind exact-head wait authorization before requesting/observing CI."""
        async with self.db.immediate() as conn:
            await conn.execute(update(integration_batches).where(
                integration_batches.c.id == batch.id,
                integration_batches.c.intent == "open",
                integration_batches.c.lifecycle != "promoted",
                integration_batches.c.repair_attempt_count == batch.repair_attempt_count,
            ).values(tested_candidate_sha=observation.candidate_sha))

    async def settle(self, batch: Batch, observation: BatchObservation) -> None:
        async with self.db.immediate() as conn:
            await self.settle_on(conn, batch, observation)
        await self.cleanup_settled(batch)

    async def settle_on(self, conn, batch: Batch, observation: BatchObservation) -> None:
        trigger = await conn.scalar(select(integration_batches.c.trigger).where(
            integration_batches.c.id == batch.id,
        ))
        if trigger == "promotion":
            from src.integration.promotion_steps import settle_promotion

            await settle_promotion(self.db, batch, observation, clock=self.clock, conn=conn)
        settled = await execute_batch_mutation(conn,
            update(integration_batches)
            .where(integration_batches.c.id == batch.id,
                   integration_batches.c.target_ref.is_not(None),
                   integration_batches.c.lifecycle != "promoted")
            .values(lifecycle="promoted", final_main_sha=observation.target_sha,
                    tested_candidate_sha=observation.candidate_sha,
                    updated_at=self.clock())
        )
        if settled.rowcount and trigger != "promotion":
            from src.integration.outbox import enqueue_integration_event

            identity = "train-published:" + batch.id
            await enqueue_integration_event(
                conn, event_id=identity, dedup_key=identity, project_id=batch.project_id,
                event_type="integration.publication_verified", available_at=self.clock(),
                payload={"batch_id": batch.id, "revision": batch.repair_attempt_count,
                         "head_sha": observation.target_sha,
                         "repository_id": batch.repository_id, "target_ref": batch.target_ref,
                         "candidate_sha": observation.candidate_sha,
                         "publication_verified": True},
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

    async def cleanup_settled(self, batch: Batch) -> None:
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
    # The stored parent checks are advisory: read and shown, never a gate. Only
    # explicit, current project playbook data requires epic checks (6.4).
    required = (policy.get("parent") or {}).get("required_checks") or {}
    explicit = (await playbook_epic_checks_on(conn, project["id"])
                if project.get("id") else None)
    if explicit is not None:
        required = explicit.model_dump(mode="json")
    boundary = "parent" if row["parent_task_id"] else "root"
    reviewed = (policy.get(boundary) or {}).get("admission") == "reviewed"
    reviewers = set()
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
        if set(names) <= set(ci.commands):
            trust = local_checks_producer_id(
                scope="epic", names=names, version=str(required.get("version") or ""),
                commands=tuple(ci.commands[name] for name in names),
                queue_seconds=ci.queue_seconds, run_seconds=ci.run_seconds,
            )
            if ci.requires_hosted("epic"):
                trust = hybrid_checks_producer_id(trust, str(required.get("producer_id") or ""))
        else:
            # local_commands_cover_required_checks: no local producer exists, so
            # a required check names its missing producer and an advisory one
            # is shown as unknown.
            trust = ""
    return EpicPolicy(tuple(required.get("names") or ()), trust,
                      ReviewRequirements(reviewed, frozenset(reviewers)),
                      str(required.get("version") or ""), explicit is not None)


async def epic_source_base_on(conn, row):
    return await conn.scalar(select(task_branch_origins.c.base_sha).where(
        task_branch_origins.c.task_id == row["id"],
    ).order_by(task_branch_origins.c.retired_at.is_(None).desc(),
               task_branch_origins.c.creation_generation.desc()).limit(1))


def epic_graph_digest(graph) -> str:
    """Fence the ordinary inputs a completion examined, excluding its own row."""
    data = asdict(graph)
    data["dependencies"] = [tuple(edge) for edge in graph.dependencies]
    for node in data["nodes"]:
        if not node["policy"]["checks_required"]:
            # Advisory is the default, so completions fenced before 6.4 stay current.
            node["policy"].pop("checks_required")
        if node["request"]["task_id"] in {graph.epic_id, graph.node(graph.epic_id).parent_id}:
            node["request"]["completion_id"] = None
            node["request"]["completed_at"] = None
            for field in ("reported_source", "completion_outcome", "completion_commits"):
                node["request"][field] = None
            node["request"]["has_recorded_source"] = False
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
            ids = list(await _epic_branches_on(conn, target=target))
            if len(ids) != 1:
                logger.info("Skipping epic completion for %s: expected one completed container, "
                            "found %s (%s)", target.key, len(ids), ids)
                return ()
            graph = await self.evaluator.graph_reader.read_on(conn, ids[0])
        node = graph.node(graph.epic_id)
        if not node.container or (graph.project_id, graph.repository_id) != target.key[:2]:
            logger.warning("Skipping epic completion for %s: task %s has mismatched container "
                           "or repository identity", target.key, graph.epic_id)
            return ()
        # Collection readiness gates work still owed; it cannot undo delivery.
        # Retired epic refs are expected after landing, so use the designated
        # root from this same fetch before examining the private branch.
        root = snapshot.for_target(graph.default_ref)
        delivered = await project_delivered(
            self.db, [node.task_id], project_id=graph.project_id,
            repository_id=graph.repository_id, target_ref=graph.default_ref, snapshot=root,
        )
        if node.task_id in delivered:
            return ({"code": "epic_delivered", "task_id": node.task_id, "ref": node.task_id,
                     "target_ref": graph.default_ref, "target_sha": root.target_oid,
                     "blocking": False, "detail": "epic is already delivered to the project root"},)
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
            # The fetched aggregate and retained completion ref already bind
            # this task/repository/head. A missing live origin is disposable
            # bookkeeping; selection still requires a real retained filing base.
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
        if first:
            return ({"code": "epic_completed", "task_id": node.task_id, "ref": node.task_id,
                     "head_sha": readiness.head_sha, "blocking": False},)
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

    @train_write
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

    @train_write
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

    @train_write
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
        self.store = BatchStore(self.db, clock=clock, require_enabled=True)
        self.publish = LeasedPublish(self.db, self.git, clock=clock)
        if self.batches is not None:
            self.batches.conflict_handler = getattr(orchestrator, "_refresh_root_pr", None)
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

    async def _checks_changed(self, head):
        """Wake readiness only after changed exact-head evidence is committed."""
        bus = getattr(self.orchestrator, "bus", None)
        if bus is not None:
            repo = await self.db.get_repo(head.repository_id)
            if repo is not None:
                await bus.emit("integration.readiness_changed", {
                    "project_id": repo.project_id, "repository_id": head.repository_id,
                    "head_sha": head.sha, "reason": "checks_changed",
                })

    async def __call__(self, target: TrainTarget) -> TrainLane:
        from src.integration.repository_git import development_repository
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

        async def observe_deferred(batch, sha):
            # A finite due observation refreshes existing CI; it neither starts
            # another run nor consumes a fixer round. Resolve current policy first.
            exact = await checks.for_candidate(batch, sha)
            if exact is None:
                return True
            from src.integration.train import candidate_head

            return (await exact.refresh(candidate_head(batch, sha))).green

        async def current_gate(batch, candidate_sha, tree_sha):
            # Rechecked inside the publisher fence, including edits made while
            # this visit was waiting for CI. Never publish using obsolete green.
            current = await self._policy(target.project_id)
            if current != policy:
                return False
            return await checks.gate(batch, candidate_sha, tree_sha)

        service = BatchService(self.store, gitops, publish=self.publish,
                               eligible=self.batches.eligible, gate=current_gate,
                               attest=None if unattested else attest,
                               require_attestation=not unattested,
                               observe_deferred=observe_deferred)

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
                if inputs is None or not inputs[0]:
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

        async def baseline_fix_checks(member):
            from src.integration.runtime_contracts import HeadIdentity

            if local or local_ci:
                exact = await resolve(batch=Batch("baseline-fix-" + member.task_id,
                    target.project_id, target.repository_id, target.target_ref),
                    candidate_sha=member.source_sha)
            else:
                exact = await self._pr_checks(target, member, policy, binding)
            if exact is None:
                return None
            task = await self.db.get_task(member.task_id)
            return await _exact_head_verdict(exact, HeadIdentity(
                repository_id=target.repository_id, ref=_branch(task.branch_name),
                sha=member.source_sha, generation=0))

        prepare_speculative = None
        train_policy = policy.get("train") or {}
        if target.kind == "root" and train_policy.get("speculative_ci"):
            from src.integration.models import IntegrationTrainPolicy

            # Validate at use as well as policy ingestion. Development/local
            # compute can never be opted in by a hosted-looking policy block.
            configured = IntegrationTrainPolicy.model_validate(train_policy)
            if local or local_ci:
                raise ValueError("speculative_ci_requires_hosted_root")
            if configured.speculative_ci:
                async def prepare_speculative(parent, base, snapshot):
                    return await self.batches.prepare_speculative(
                        target, service, parent, base, snapshot)

        from src.integration.train_policy import load_train_policy

        view = await load_train_policy(self.db, target.project_id)
        return TrainLane(snapshot=snapshot, service=service, checks=checks,
                         train_policy=view.policy, train_policy_enabled=view.enabled,
                         train_policy_reason=view.reason,
                         repair_policy=(RepairPolicy.model_validate(repair_policy)
                                        if repair_policy else None),
                         complete_epic=complete_epic, sync_default_branch=sync_default_branch,
                         sync_closed_epic=sync_closed_epic,
                         baseline_fix_checks=baseline_fix_checks,
                         prepare_speculative=prepare_speculative)

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

    async def _advisory_epic_checks(self, target, binding, retained, ci, policy, node, batch,
                                    head, repository_id, resolved):
        """Show recorded advisory results; never run, wait for or refuse on one.

        An advisory epic check holds nothing (6.4): a local result is read from
        evidence a train already recorded, a hosted one is observed, and an
        unavailable producer leaves the head's result unknown.
        """
        from src.integration.runtime_contracts import HeadIdentity

        resolved[(head, node.policy)] = None
        try:
            if ci is not None and ci.source_for("epic") != "hosted":
                exact = self._local_checks(target, retained.store, ci, policy, batch)
            else:
                exact = await self._hosted(policy, binding, target, batch, head)
                if exact is not None:
                    await exact.refresh(HeadIdentity(repository_id=repository_id,
                                                     ref=node.branch_ref, sha=head, generation=0))
        except Exception:
            logger.info("Advisory checks for epic %s at %s are unavailable", node.task_id, head,
                        exc_info=True)
            return
        resolved[(head, node.policy)] = exact

    def _epic_completion(self, target, binding, retained):
        from src.integration.checks import HostedChecks, HybridChecks
        from src.integration.runtime_contracts import HeadIdentity

        resolved = {}

        async def refresh(graph, snapshot):
            policy = await self._policy(target.project_id)
            async with self.db._engine.connect() as conn:
                explicit = await playbook_epic_checks_on(conn, target.project_id)
            if explicit is not None:
                # The playbook's parent checks replace the stored advisory set.
                policy = {**policy, "parent": {**(policy.get("parent") or {}),
                                               "required_checks": explicit.model_dump(mode="json")}}
            ci = integration_ci_policy(policy)
            local = ci is not None and ci.source_for("epic") != "hosted"
            refused: list[dict] = []
            pending_checks = []
            for node in graph.nodes:
                head = snapshot.for_target(node.branch_ref).target_oid if node.branch_ref else None
                if not node.required or not node.container or not head:
                    continue
                if not node.policy.check_names:
                    resolved[(head, node.policy)] = None
                    continue
                batch = Batch("epic-readiness-" + node.task_id, target.project_id,
                              target.repository_id, node.branch_ref)
                if not node.policy.checks_required:
                    await self._advisory_epic_checks(target, binding, retained, ci, policy, node,
                                                     batch, head, graph.repository_id, resolved)
                    continue
                if not node.policy.check_trust:
                    # The evaluator names the missing producer; nothing can run.
                    resolved[(head, node.policy)] = None
                    continue
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
                    pending_checks.append((exact, HeadIdentity(
                        repository_id=graph.repository_id, ref=node.branch_ref,
                        sha=head, generation=0)))
            # Conclusive red heads retain the existing bounded baseline-sync
            # and repair path. A refresh must not allocate a parallel repair.
            node = graph.node(graph.epic_id)
            exact = resolved.get((snapshot.target_oid, node.policy))
            known_red = exact is not None and (await _exact_head_verdict(exact, HeadIdentity(
                repository_id=graph.repository_id, ref=target.target_ref,
                sha=snapshot.target_oid, generation=0))).state.value == "red"
            # A closed, collected epic may predate the default branch's trust
            # configuration. Refresh it before requesting aggregate checks;
            # waiting for a conflicting root PR cannot help until those checks
            # have produced the very completion that opens the PR.
            if (not snapshot.error and snapshot.target_oid and not known_red
                    and await self.batches.sync_inputs(target, snapshot) is not None):
                from src.integration.stacked_branches import EpicRefresh

                try:
                    refreshed = await _finish_on_cancel(EpicRefresh(
                        self.db, clock=self.clock).start(graph.epic_id, snapshot=snapshot))
                except (GitError, OSError, ValueError) as exc:
                    logger.warning("Collected epic refresh unavailable for %s", graph.epic_id,
                                   exc_info=True)
                    return ({"code": "epic_refresh_unavailable", "task_id": graph.epic_id,
                             "ref": target.target_ref, "reason": type(exc).__name__,
                             "detail": "Collected epic refresh will be retried"},)
                if refreshed["outcome"] not in {"current", "skipped"}:
                    if refreshed["outcome"] == "started":
                        await self.db.log_event("integration.epic_refresh",
                            project_id=target.project_id, task_id=graph.epic_id,
                            payload=json.dumps({**refreshed, "trigger": "aggregate_readiness"}))
                    return ({"code": "epic_refresh_pending", "task_id": graph.epic_id,
                             "ref": target.target_ref, "refresh": refreshed,
                             "detail": "Aggregate verification waits for the default-branch refresh"},)
            for exact, identity in pending_checks:
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

        evaluator = EpicReadinessEvaluator(self.db, EpicGraphReader(self.db,
            policy_on=epic_policy_on, source_base_on=epic_source_base_on,
        ), TreeReviews(self.db, allow_missing=True), checks=head_checks)
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
            return ExactChecks(self.db, HostedChecks(HostedCIProducer(client, selected)),
                               on_change=self._checks_changed)

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
            FlowSchema,
            PromotionSourceRefusal,
            check_source_green,
            promotion_policy_event,
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
        from src.integration.train_policy import current_checks, load_train_policy

        return current_checks(policy if isinstance(policy, dict) else {},
                              await load_train_policy(self.db, project_id))

    async def _settings(self, policy: dict) -> tuple[Any, str | None]:
        from src.integration.development_policy import (
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
        from src.git.github_contracts import GitHubCredentialMode, credential_identity_from_client
        from src.integration.attestation import _MAX_TRUST_BYTES, _parse_trust_manifest
        from src.integration.ci import TRUST_MANIFEST_PATH

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
            store=store, plan=plan, clock=self.clock, workspace_batch_id=batch.id,
        )
        checks = RequestedChecks if requested else ExactChecks
        return checks(self.db, LocalChecks(producer, project_id=target.project_id,
                                           names=names, version=version,
                                           scope=("promotion:" + str(target.step_id)
                                                  if target.kind == "promotion"
                                                  else _ci_kind(target))), clock=self.clock,
                      on_change=self._checks_changed)

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
            store=retained.store, plan=plan, clock=self.clock, workspace_batch_id=batch.id,
        )
        return ExactChecks(self.db, LocalChecks(producer, project_id=target.project_id),
                           on_change=self._checks_changed)

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
        ), clock=self.clock, on_change=self._checks_changed)

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

    def wake(self, project_id, repository_id=None):
        return self.train.wake(project_id, repository_id)

    def set_wake_callback(self, callback):
        self.train.on_wake = callback

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
    from src.integration.dispatch_waits import DispatchWaits
    from src.integration.fixer_runtime import DaemonFixerRuntime
    from src.integration.fixers import BatchFixerService

    async def refresh_conflict(batch_id_, record):
        handler = getattr(orchestrator, "_command_handler", None)
        if handler is None:
            return {"success": False, "outcome": "unavailable"}
        return await handler.refresh_integration_pair_conflict(batch_id_, record)

    batches = DatabaseBatches(orchestrator.db, clock=clock,
                             cleanup=getattr(orchestrator, "integration_cleanup_service", None),
                             refresh_conflict=refresh_conflict)
    lanes = DaemonLanes(orchestrator, batches=batches, clock=clock)
    waits = DispatchWaits(orchestrator, lanes=lanes, clock=clock)
    return IntegrationTrain(
        targets=DatabaseTargets(
            orchestrator.db,
            flow_problems=lambda: getattr(orchestrator, "promotion_flow_problems", None),
        ),
        batches=batches,
        lane_for=lanes,
        repair=BatchFixerService(
            orchestrator.db, DaemonFixerRuntime(orchestrator, lanes=lanes, clock=clock),
            clock=clock,
        ),
        baseline=CandidateBaselineService(orchestrator.db, clock=clock),
        event_driven=True,
        waits=waits,
        clock=clock,
    )
