"""Git delivery truth for the readers and guards outside the publisher.

Stale-container settlement, integration status, archive/removal guards,
branch cleanup and the legacy diagnostic readers all ask one question: is a
completed task's *current* work on its project's configured target?  They ask
it here, of :mod:`src.integration.delivery_truth`, never of a delivery row.

The division of labour follows that module's contract:

* SQL decides only *which* tasks need proof (:func:`development_delivery_scope`),
  a graph/identity fact.  Whether their work is on the target is git's answer.
* :class:`DeliveryObserver` fetches an isolated store per repository, never the
  publisher's working checkout, so a reader neither waits for nor disturbs the
  long publisher lock.  It runs outside every database transaction.
* A guarded writer then rechecks, on its own transaction and under its own
  locks, that each evaluated identity is unchanged
  (:meth:`DeliveryView.verified_on`) and that the target has not moved since
  the fetch (:meth:`DeliveryView.fresh`).  Anything unverified is unknown, and
  unknown fails closed: it never settles, archives or deletes.

Nothing here is persisted. A view lives for one request; the truth cache may
reuse immutable Git proofs for identical completion inputs and pinned OIDs.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import weakref
from collections.abc import Iterable, Mapping
from functools import partial
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType

from sqlalchemy import literal, or_, select

from src.database.tables import (
    archived_tasks,
    projects,
    repos,
    task_completion_records,
    task_branch_origins,
    tasks,
)
from src.git.manager import GitError, RemoteRefState
from src.integration.delivery_truth import (
    DeliveryEvidence,
    DeliveryRequest,
    DeliverySnapshot,
    DeliveryState,
    delivery_snapshot,
    load_delivery_requests,
)

logger = logging.getLogger(__name__)

def development_delivery_scope(task):
    """``EXISTS``: *task*'s completed work must reach its project's target.

    The graph half of the old ``_development_delivery_pending``: a development
    project with a designated repository, the task on that repository, unpinned,
    or on one that is not its project's own (``fleet-meadow``: nobody collects
    it, so git can never prove it and it stays unknown until rebound), and not
    closed obsolete.  Another repository of the same project is a deliberate
    binding outside the publisher and needs no proof.

    A branchless task needs proof only when it recorded an artifact: a
    completion that listed commits, or a current generation the
    ``development_deliveries`` retirement marked as naming a historical source
    it never recorded (:func:`~src.integration.publishable_artifact.legacy_artifact`,
    unknown until migrated).  Anything else is an organizational container with
    no artifact of its own, exactly as
    :class:`~src.integration.delivery_truth.DeliverySnapshot` rules.
    """
    from src.database.queries.blocked_state import obsolete_marker
    from src.integration.publishable_artifact import legacy_artifact

    project = projects.alias()
    repo = repos.alias()
    own = repos.alias()
    completion = task_completion_records.alias()
    recorded = (
        select(literal(1))
        .where(completion.c.task_id == task.c.id, completion.c.commits != "[]")
        .correlate(task)
        .exists()
    )
    return (
        select(literal(1))
        .select_from(project.join(repo, repo.c.id == project.c.integration_repository_id))
        .where(
            project.c.id == task.c.project_id,
            project.c.hierarchical_integration_mode == "development",
            or_(
                task.c.repo_id.is_(None),
                task.c.repo_id == repo.c.id,
                ~select(literal(1))
                .where(own.c.id == task.c.repo_id, own.c.project_id == task.c.project_id)
                .correlate(task)
                .exists(),
            ),
            or_(task.c.branch_name.is_not(None), recorded, legacy_artifact(task)),
            ~obsolete_marker(task),
        )
        .correlate(task)
        .exists()
    )


async def delivery_sensitive_ids(conn, task_ids: Iterable[str]) -> set[str]:
    """The COMPLETED tasks among *task_ids* whose delivery git must prove."""
    ids = sorted(set(task_ids))
    if not ids:
        return set()
    rows = await conn.execute(
        select(tasks.c.id).where(
            tasks.c.id.in_(ids),
            tasks.c.status == "COMPLETED",
            development_delivery_scope(tasks),
        )
    )
    return set(rows.scalars())


#: Bound on the completed tasks one status read asks git about.
STATUS_DELIVERY_LIMIT = 200


async def gating_delivery_ids(conn, project_id: str, *, limit: int = STATUS_DELIVERY_LIMIT):
    """Completed development work that open work waits on, newest first.

    What the scheduler and settlement wait for: the prerequisite of a
    ``blocks`` edge whose dependent is still open, and the child of a
    container that has not finished.  Returns ``(ids, total)`` with *ids*
    bounded by *limit*, so a status read stays one bounded git pass.
    """
    from src.database.tables import task_dependencies

    finished = ("COMPLETED", "FAILED", "CANCELLED")
    dependent = tasks.alias("gating_dependent")
    parent = tasks.alias("gating_parent")
    edge = task_dependencies.alias("gating_edge")
    gating = or_(
        select(literal(1))
        .select_from(edge.join(dependent, dependent.c.id == edge.c.task_id))
        .where(
            edge.c.depends_on_task_id == tasks.c.id,
            edge.c.dep_type == "blocks",
            dependent.c.status.not_in(finished),
        )
        .correlate(tasks)
        .exists(),
        select(literal(1))
        .where(parent.c.id == tasks.c.parent_task_id, parent.c.status.not_in(finished))
        .correlate(tasks)
        .exists(),
    )
    rows = (
        await conn.execute(
            select(tasks.c.id)
            .where(
                tasks.c.project_id == project_id,
                tasks.c.status == "COMPLETED",
                development_delivery_scope(tasks),
                gating,
            )
            .order_by(tasks.c.updated_at.desc(), tasks.c.id)
        )
    ).scalars().all()
    return list(rows[:limit]), len(rows)


@dataclass(frozen=True)
class DeliveryTarget:
    """Where one project's work is delivered: its designated repository's default ref."""

    project_id: str
    repository_id: str
    repository_url: str
    target_ref: str


async def delivery_targets(conn, task_ids: Iterable[str], *, reduced=False) -> dict[str, DeliveryTarget]:
    """Current ordinary project/parent targets for live and archived tasks."""
    ids = sorted(set(task_ids))
    found: dict[str, DeliveryTarget] = {}
    if not ids:
        return found
    parent = tasks.alias("delivery_parent")
    for table in (tasks, archived_tasks):
        rows = await conn.execute(select(
            table.c.id, projects.c.id.label("project_id"), repos.c.id.label("repository_id"),
            repos.c.url, repos.c.default_branch,
        ).select_from(table.join(projects, projects.c.id == table.c.project_id).join(
            repos, repos.c.id == projects.c.integration_repository_id,
        )).where(table.c.id.in_(ids)))
        for row in rows.mappings():
            found.setdefault(row["id"], DeliveryTarget(
                row["project_id"], row["repository_id"], row["url"],
                "refs/heads/" + str(row["default_branch"]).removeprefix("refs/heads/"),
            ))
        if reduced:
            rows = (await conn.execute(select(table.c.id, parent.c.branch_name).select_from(
                table.join(parent, parent.c.id == table.c.parent_task_id),
            ).where(table.c.id.in_(ids), parent.c.project_id == table.c.project_id,
                    parent.c.branch_name.is_not(None)))).all()
            for task_id, parent_ref in rows:
                if task_id in found:
                    target = found[task_id]
                    found[task_id] = DeliveryTarget(
                        target.project_id, target.repository_id, target.repository_url,
                        "refs/heads/" + str(parent_ref).removeprefix("refs/heads/"),
                    )
    # A hotfix's recorded origin selects its chain target even for ordinary
    # delivery readers. Arbitrary parent refs retain the existing routing.
    from src.integration.promotion_routing import promotion_origin_target

    origins = (await conn.execute(select(task_branch_origins).where(
        task_branch_origins.c.task_id.in_(ids), task_branch_origins.c.retired_at.is_(None),
    ))).mappings().all()
    configs = {row.id: row for row in (await conn.execute(select(
        projects.c.id, projects.c.promotion_flow, projects.c.default_branch_cutover).where(
        projects.c.id.in_({target.project_id for target in found.values()}),
    ))).all()}
    for origin in origins:
        task_id = origin["task_id"]
        target = found.get(task_id)
        if target is None:
            continue
        config = configs[target.project_id]
        branch = promotion_origin_target(target.target_ref, config.promotion_flow, origin,
                                         target.repository_id, config.default_branch_cutover)
        found[task_id] = DeliveryTarget(target.project_id, target.repository_id,
                                        target.repository_url, "refs/heads/" + branch)
    return found


def _unknown(request: DeliveryRequest, reason: str, snapshot=None) -> DeliveryEvidence:
    return DeliveryEvidence(
        request, DeliveryState.UNKNOWN,
        getattr(snapshot, "target_oid", None), None, reason,
    )


@dataclass
class DeliveryView:
    """Evidence for one request, and the checks a guarded writer repeats.

    ``evidence`` maps task id to what git said about the identity evaluated;
    a task with no entry was never evaluated (no target, or not asked).
    """

    db: object = None
    evidence: Mapping[str, DeliveryEvidence] = field(default_factory=dict)
    targets: Mapping[str, DeliveryTarget] = field(default_factory=dict)
    snapshots: tuple[DeliverySnapshot, ...] = ()
    request_loader: object = load_delivery_requests
    target_loader: object = delivery_targets

    def get(self, task_id: str) -> DeliveryEvidence | None:
        return self.evidence.get(task_id)

    def satisfied(self, task_id: str) -> bool:
        evidence = self.evidence.get(task_id)
        return evidence is not None and evidence.satisfied

    async def fresh(self) -> bool:
        """Every inspected target is still at the OID this view evaluated against.

        A snapshot that failed already answered unknown for all of its tasks,
        so it has no target to go stale and does not spoil its peers.
        """
        groups = {}
        for snapshot in self.snapshots:
            if snapshot.error is None:
                key = snapshot.git, snapshot.store, snapshot.repository_url
                groups.setdefault(key, []).append(snapshot)
        fresh = True
        for (git, store, url), snapshots in groups.items():
            branches = sorted({s.target_ref.removeprefix("refs/heads/") for s in snapshots})
            try:
                refs = (await git.als_remote_refs(store, branches)
                        if await git.aget_remote_url(store) == url else {})
            except (GitError, OSError):
                refs = {}
            for snapshot in snapshots:
                ref = refs.get(snapshot.target_ref.removeprefix("refs/heads/"))
                current = bool(ref and ref.state is RemoteRefState.PRESENT
                               and ref.oid == snapshot.target_oid)
                snapshot._freshness[snapshot.target_ref] = current
                fresh &= current
        return fresh

    async def verified_on(self, conn, task_ids: Iterable[str]) -> dict[str, DeliveryEvidence]:
        """Evidence whose identity and target still hold on *conn*.

        A guarded writer calls this inside its transaction.  A task reopened,
        re-completed, re-targeted or rebound since the observation is left
        out: its evidence answered an earlier question, so the caller treats
        it as unknown and retries on a later pass.
        """
        ids = {task_id for task_id in task_ids if task_id in self.evidence}
        if not ids:
            return {}
        current_targets = await self.target_loader(conn, ids)
        groups: dict[DeliveryTarget, set[str]] = {}
        for task_id in ids:
            target = current_targets.get(task_id)
            if target is not None and target == self.targets.get(task_id):
                groups.setdefault(target, set()).add(task_id)
        verified: dict[str, DeliveryEvidence] = {}
        for target, group in groups.items():
            current = await self.request_loader(
                self.db, group, repository_id=target.repository_id,
                target_ref=target.target_ref, conn=conn,
            )
            for task_id in group:
                evidence = self.evidence[task_id]
                if current.get(task_id) == evidence.request:
                    base = getattr(evidence, "source_base", None)
                    if base is not None:
                        current_base = await conn.scalar(select(task_branch_origins.c.base_sha).where(
                            task_branch_origins.c.task_id == task_id,
                            task_branch_origins.c.repository_id == target.repository_id,
                            task_branch_origins.c.retired_at.is_(None),
                        ))
                        if current_base != base:
                            continue
                    verified[task_id] = evidence
        return verified


@dataclass
class PrerequisiteView:
    """Sibling delivery and default-branch delivery from the same graph read."""

    siblings: DeliveryView
    default: DeliveryView
    #: Dependent task id -> the cross-parent prerequisites it waits on, each
    #: with whether its exact default-branch source is already contained in
    #: the dependent's own parent branch (so no epic refresh is needed).
    parent_containment: Mapping[str, Mapping[str, bool]] = field(default_factory=dict)
    parent_snapshots: tuple[DeliverySnapshot, ...] = ()

    def get(self, task_id):
        return self.siblings.get(task_id)

    def satisfied(self, task_id):
        return self.siblings.satisfied(task_id)

    @property
    def evidence(self):
        return self.siblings.evidence

    @property
    def targets(self):
        return self.siblings.targets

    @property
    def all_ids(self):
        return self.siblings.evidence.keys() | self.default.evidence.keys()

    @property
    def snapshots(self):
        return self.siblings.snapshots + self.default.snapshots + self.parent_snapshots

    async def fresh(self):
        return await DeliveryView(snapshots=self.snapshots).fresh()

    async def parents_fresh(self):
        return await DeliveryView(snapshots=tuple(
            snapshot for snapshot in self.parent_snapshots if snapshot.error is None
        )).fresh()

    async def verified_on(self, conn, ids):
        return await self.siblings.verified_on(conn, ids)

    async def mode(self, mode, *, conn=None):
        from dataclasses import replace

        siblings, default = self.siblings.evidence, self.default.evidence
        if conn is not None:
            siblings = await self.siblings.verified_on(conn, self.all_ids)
            default = await self.default.verified_on(conn, self.all_ids)
        default_ids = frozenset(tid for tid, p in default.items() if p.satisfied)
        return replace(mode,
            delivered_prerequisite_ids=frozenset(tid for tid, p in siblings.items() if p.satisfied),
            default_prerequisite_ids=default_ids,
            parent_contained_task_ids=frozenset(
                dependent for dependent, sources in self.parent_containment.items()
                if sources and all(contained and tid in default_ids
                                   for tid, contained in sources.items())
            ))


_FETCH_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def prerequisite_observer(db):
    """The truth-bearing observer for sibling prerequisites, or None in shadow mode.

    The daemon's general delivery observer carries no ``GitTruth``; under
    ``integration.git_first: active`` it registers a separate one
    (:meth:`set_prerequisite_observer`).  A delivery observer that already
    carries truth (tests, embedders) is used directly.
    """
    observer = getattr(db, "_prerequisite_observer", None)
    if observer is not None and getattr(observer, "truth", None) is not None:
        return observer
    observer = getattr(db, "_delivery_observer", None)
    if observer is not None and getattr(observer, "truth", None) is not None:
        return observer
    return None


async def hierarchy_frontier_modes(
    db, *, project_ids=None, task_id=None, cached_only=False, display_unavailable=None,
):
    """Request-scoped Git prerequisite evidence for scheduling and diagnostics.

    A cycle shares these modes between the scheduler and pool measurement.
    Direct readers take their own view. Git runs outside transactions; identity
    revalidation uses a short connection without locks. Advisory readers reuse
    successful snapshots within READ_MAX_AGE, still checking target freshness.
    Claim activation fetches and revalidates its own view under the task locks.
    ``cached_only`` pins existing evidence for interactive diagnostics without
    fetching or checking remote freshness; database identities are still checked.
    ``display_unavailable`` optionally receives cache misses by project and target
    kind. It is diagnostic metadata, never part of the admission modes.

    Shadow observers preserve receipt admission. An unstable active view supplies
    an empty delivered set, so it cannot fall back to a stale receipt.
    """
    from dataclasses import replace

    from src.database.queries.hierarchy_queries import ProjectIntegrationMode

    observer = prerequisite_observer(db)
    if observer is None:
        return {}
    if task_id is not None:
        task = await db.get_task(task_id)
        if task is None or (project_ids is not None and task.project_id not in project_ids):
            return {}
        project_ids = {task.project_id}
    candidates = await db.list_projects() if project_ids is None else [
        project for pid in sorted(project_ids)
        if (project := await db.get_project(pid)) is not None
    ]
    modes = {}
    for project in candidates:
        mode = ProjectIntegrationMode.of(project)
        if not mode.hierarchical:
            continue
        timeout = (FRONTIER_DISPLAY_TIMEOUT_SECONDS if cached_only
                   else FRONTIER_MODE_TIMEOUT_SECONDS)
        try:
            modes[project.id] = await asyncio.wait_for(_project_frontier_mode(
                db, observer, project, mode, task_id=task_id, cached_only=cached_only,
                display_unavailable=display_unavailable,
            ), timeout)
        except TimeoutError:
            # Fail closed for this read: no Git-proven prerequisite admits work,
            # but one slow Git view never holds the scheduler, pools or explain.
            logger.warning(
                "hierarchy frontier for %s%s exceeded %.0fs; withholding Git-gated work",
                project.id, f" task {task_id}" if task_id else "", timeout,
            )
            modes[project.id] = replace(
                mode, delivered_prerequisite_ids=frozenset(),
            )
    return modes


#: Wall-clock bound on one project's Git prerequisite view in a scheduler,
#: pool or claim read. Exceeding it withholds Git-gated work for that read.
FRONTIER_MODE_TIMEOUT_SECONDS = 60.0
#: Interactive diagnostics (explain, pool display) read cached evidence only and
#: must answer inside a CLI request.
FRONTIER_DISPLAY_TIMEOUT_SECONDS = 10.0


async def _project_frontier_mode(
    db, observer, project, mode, *, task_id, cached_only, display_unavailable=None,
):
    from dataclasses import replace

    view = await observer.prerequisite_view(
        project.id, task_id=task_id, max_age=observer.READ_MAX_AGE,
        **({"cached_only": True} if cached_only else {}),
    )
    verified_mode = replace(mode, delivered_prerequisite_ids=frozenset())
    if cached_only:
        if display_unavailable is not None:
            unavailable = {
                kind: frozenset(tid for tid, proof in delivery.evidence.items()
                                if proof.reason == "snapshot_unavailable")
                for kind, delivery in (("siblings", view.siblings), ("default", view.default))
            }
            if any(unavailable.values()):
                display_unavailable[project.id] = unavailable
        fresh = all(snapshot._freshness.get(snapshot.target_ref, True)
                    for snapshot in view.snapshots)
    else:
        fresh = await view.fresh()
    if fresh:
        async with db._engine.connect() as conn:
            verified_mode = await view.mode(mode, conn=conn)
    stackable = frozenset()
    if mode.stacked:
        from src.integration.stacked_branches import observe_stacks

        stack_view = await observe_stacks(observer, project.id, task_id=task_id,
                                           max_age=observer.READ_MAX_AGE,
                                           **({"cached_only": True} if cached_only else {}))
        if await stack_view.fresh():
            async with db._engine.connect() as conn:
                stackable = frozenset(await stack_view.verified_on(conn))
    return replace(verified_mode, stackable_prerequisite_ids=stackable)


def _fetch_lock(path: Path) -> asyncio.Lock:
    """One in-process lock per observer store, so fetches never race each other."""
    locks = _FETCH_LOCKS.setdefault(asyncio.get_running_loop(), {})
    return locks.setdefault(str(path), asyncio.Lock())


class DeliveryObserver:
    """Fetch and evaluate delivery truth for arbitrary tasks, outside transactions.

    Each repository gets an observer clone beside, never inside, the
    publisher's retained checkout. The reduced observer fetches once per
    repository and shares its refs across targets. A read-only surface may reuse
    a snapshot within :data:`READ_MAX_AGE`. Reduced diagnostics consume only
    existing OID-bound proofs; guarded writers still fetch and recheck inputs.
    """

    #: Attempts to take a view whose targets did not move while it was evaluated.
    FRESH_ATTEMPTS = 2
    #: How old a fetched snapshot a read-only surface (status, explain, doctor)
    #: may reuse.  The dashboard polls explain every 20s; this keeps that from
    #: becoming a fetch per poll.  Guarded writers never reuse one.
    READ_MAX_AGE = 30.0

    def __init__(self, db, *, git, data_dir, truth=None) -> None:
        self.db = db
        self.git = git
        self.truth = truth
        self.request_loader = partial(load_delivery_requests, reduced=truth is not None)
        self.target_loader = partial(delivery_targets, reduced=truth is not None)
        self.data_dir = Path(data_dir) / "development-integration"
        self._recent: dict[DeliveryTarget, tuple[float, DeliverySnapshot]] = {}
        self._stack_ref_cache: dict = {}

    def store_path(self, repository_id: str) -> Path:
        digest = hashlib.sha256(repository_id.encode()).hexdigest()[:20]
        return self.data_dir / digest / "observer"

    async def _store(self, target: DeliveryTarget) -> Path:
        path = self.store_path(target.repository_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            await self.git.acreate_checkout(target.repository_url, str(path), no_checkout=True)
        return path

    async def _snapshot(
        self, target: DeliveryTarget, max_age: float = 0.0, *, cached_only: bool = False,
    ) -> DeliverySnapshot:
        """A fetched snapshot, or an unknown one: a failure is never an empty answer.

        *max_age* lets a read-only caller reuse a successful snapshot of the
        same target fetched that recently; it still pins one exact target OID.
        """
        path = self.store_path(target.repository_id)
        recent = self._recent.get(target)
        if (max_age > 0 and recent is not None and time.monotonic() - recent[0] <= max_age
            and (recent[1].error is None or cached_only)
            and (not cached_only or recent[1]._freshness.get(target.target_ref, True))):
            return recent[1]
        if cached_only:
            # Interactive diagnostic reads must never queue behind a fetch or start
            # one. Expired evidence is unknown, not evidence of non-delivery.
            return DeliverySnapshot(
                self.git, str(path), target.project_id, target.repository_id,
                target.repository_url, target.target_ref, None, MappingProxyType({}),
                "snapshot_unavailable",
            )
        try:
            async with _fetch_lock(path):
                store = await self._store(target)
                if self.truth is not None:
                    snapshot = (await self.truth.snapshot(
                        str(store), project_id=target.project_id,
                        repository_id=target.repository_id, repository_url=target.repository_url,
                        target_ref=target.target_ref,
                    )).observation
                else:
                    snapshot = await delivery_snapshot(
                        self.git, store, project_id=target.project_id,
                        repository_id=target.repository_id,
                        repository_url=target.repository_url, target_ref=target.target_ref,
                    )
            if snapshot.error in {None, "missing_target"}:
                # The fetch captured every remote branch. Preserve its timestamp
                # for every covered target, including previously cached branches
                # that have since disappeared, without extending reused evidence.
                stamp = time.monotonic()
                covered = {target} | {
                    cached for cached in self._recent
                    if (cached.project_id, cached.repository_id, cached.repository_url)
                    == (target.project_id, target.repository_id, target.repository_url)
                } | {
                    replace(target, target_ref="refs/heads/" + ref.removeprefix(
                        "refs/remotes/origin/"))
                    for ref in snapshot.source_heads
                    if ref.startswith("refs/remotes/origin/") and ref != "refs/remotes/origin/HEAD"
                }
                for covered_target in covered:
                    oid = snapshot.source_heads.get("refs/remotes/origin/" +
                                                    covered_target.target_ref.removeprefix(
                                                        "refs/heads/"))
                    self._recent[covered_target] = (stamp, replace(
                        snapshot, target_ref=covered_target.target_ref, target_oid=oid,
                        error=None if oid else "missing_target",
                    ))
            return snapshot
        except Exception as exc:  # noqa: BLE001 - any failure is an unknown observation
            logger.warning(
                "delivery observer: %s %s unavailable: %s",
                target.repository_id, target.target_ref, exc,
            )
            return DeliverySnapshot(
                self.git, str(path), target.project_id, target.repository_id,
                target.repository_url, target.target_ref, None, MappingProxyType({}),
                f"observer_error: {type(exc).__name__}",
            )

    async def snapshot(
        self, target: DeliveryTarget, *, max_age: float = 0.0, cached_only: bool = False,
    ) -> DeliverySnapshot:
        """One isolated, request-scoped snapshot; diagnostics can require a cached read."""
        snapshot = (await self._snapshot(target, max_age, cached_only=cached_only)).for_request()
        if self.truth is not None:
            from src.integration.git_truth import GitTruthSnapshot

            return GitTruthSnapshot(self.truth, snapshot, cached_only=cached_only)
        return snapshot

    async def _evaluate(self, target: DeliveryTarget, task_ids: set[str], max_age: float = 0.0,
                        *, snapshot=None, cached_only: bool = False):
        if snapshot is None:
            snapshot = await self._snapshot(target, max_age, cached_only=cached_only)
        snapshot = snapshot.for_request()
        requests = await self.request_loader(
            self.db, task_ids, repository_id=target.repository_id,
            target_ref=target.target_ref,
        )
        try:
            if self.truth is not None:
                from src.integration.git_truth import GitTruthSnapshot

                async with self.db._engine.connect() as conn:
                    bases = dict((await conn.execute(select(
                        task_branch_origins.c.task_id, task_branch_origins.c.base_sha,
                    ).where(task_branch_origins.c.task_id.in_(task_ids),
                            task_branch_origins.c.repository_id == target.repository_id,
                            task_branch_origins.c.retired_at.is_(None)))).all())
                truth = GitTruthSnapshot(self.truth, snapshot, cached_only=cached_only)
                evaluated = {tid: await truth.is_delivered(request, source_base=bases.get(tid))
                             for tid, request in requests.items()}
            else:
                evaluated = await snapshot.evaluate_many(requests.values())
        except Exception as exc:  # noqa: BLE001 - a broken observation is unknown
            logger.warning("delivery observer: evaluation failed: %s", exc)
            evaluated = {
                task_id: _unknown(request, f"observer_error: {type(exc).__name__}", snapshot)
                for task_id, request in requests.items()
            }
        return snapshot, {
            task_id: evaluated[task_id] for task_id in task_ids if task_id in evaluated
        }

    async def prerequisite_view(
        self, project_id: str, *, task_id: str | None = None, max_age: float = 0.0,
        cached_only: bool = False,
    ):
        """Completed prerequisites, routed to the parent or default branch.

        Claims keep the default max_age=0 to fetch before revalidating under locks.
        """
        from src.database.tables import task_dependencies

        dependent = tasks.alias("delivery_dependent")
        dependent_parent = tasks.alias("delivery_dependent_parent")
        query = select(tasks.c.id, tasks.c.parent_task_id,
                       dependent.c.parent_task_id.label("dependent_parent"),
                       dependent.c.id.label("dependent_id"),
                       dependent_parent.c.branch_name.label("parent_branch"),
                       ).select_from(task_dependencies.join(
            tasks, tasks.c.id == task_dependencies.c.depends_on_task_id,
        ).join(dependent, dependent.c.id == task_dependencies.c.task_id).outerjoin(
            dependent_parent, dependent_parent.c.id == dependent.c.parent_task_id,
        )).where(
            dependent.c.project_id == project_id,
            dependent.c.status.in_(("READY", "IN_PROGRESS")),
            tasks.c.status == "COMPLETED", task_dependencies.c.dep_type == "blocks",
        )
        if task_id is not None:
            query = query.where(dependent.c.id == task_id)
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(query)).all()
        siblings = {tid for tid, parent, child_parent, _dep, _branch in rows
                    if parent is not None and parent == child_parent}
        cross = {tid for tid, parent, child_parent, _dep, _branch in rows
                 if parent is None or parent != child_parent}
        for _attempt in range(self.FRESH_ATTEMPTS):
            default = await self.observe(cross, max_age=max_age, target_loader=delivery_targets,
                                         cached_only=cached_only)
            containment, parents = await self._parent_containment(
                rows, default, cached_only=cached_only,
            )
            view = PrerequisiteView(
                await self.observe(siblings, max_age=max_age, cached_only=cached_only),
                default, containment, parents,
            )
            if cached_only or await view.parents_fresh():
                return view
            if not await default.fresh() or not await view.siblings.fresh():
                return view
            # An epic may advance while main stays fixed. Retake its shared
            # repository snapshot instead of retaining a stale containment answer.
            max_age = 0.0
        return view

    async def _parent_containment(
        self, rows, default, *, cached_only=False,
    ) -> tuple[dict[str, dict[str, bool]], tuple[DeliverySnapshot, ...]]:
        """Whether each cross-parent prerequisite is already in the dependent's epic.

        A child whose epic branch already contains every cross-epic
        prerequisite's exact default-branch source needs no epic refresh, so an
        unrelated open batch on that epic cannot withhold it. Anything not
        proven here stays False and keeps the refresh rule.
        """
        from src.integration.delivery_truth import DeliveryState

        result: dict[str, dict[str, bool]] = {}
        parents: dict[tuple[str, str, str], DeliverySnapshot] = {}
        if self.truth is None:
            return result, ()
        from src.integration.git_truth import GitTruthSnapshot

        snapshots = {(s.project_id, s.repository_id): s for s in default.snapshots}
        for tid, parent, child_parent, dependent_id, branch in rows:
            if child_parent is None or (parent is not None and parent == child_parent):
                continue
            sources = result.setdefault(dependent_id, {})
            sources[tid] = False
            proof = default.evidence.get(tid)
            if proof is None or not branch:
                continue
            if proof.state is DeliveryState.NO_ARTIFACT:
                sources[tid] = True
                continue
            snapshot = snapshots.get((proof.request.project_id, proof.request.repository_id))
            if not proof.satisfied or not proof.source_oid or snapshot is None:
                continue
            parent_ref = "refs/heads/" + branch.removeprefix("refs/heads/")
            try:
                observed = GitTruthSnapshot(self.truth, snapshot,
                                            cached_only=cached_only).for_target(parent_ref)
                parents[(snapshot.project_id, snapshot.repository_id, parent_ref)] = (
                    observed.observation
                )
                contained = await observed.is_delivered(
                    replace(proof.request, target_ref=parent_ref),
                    source_base=getattr(proof, "source_base", None),
                )
            except Exception as exc:  # noqa: BLE001 - unproven keeps the refresh rule
                logger.debug("parent containment of %s for %s unknown: %s",
                             tid, dependent_id, exc)
                continue
            sources[tid] = bool(contained.satisfied and contained.source_oid == proof.source_oid)
        return result, tuple(parents.values())

    async def observe(self, task_ids: Iterable[str], *, max_age: float = 0.0,
                      target_loader=None, cached_only: bool = False) -> DeliveryView:
        """Evaluate each task's current completion against its project's target.

        A view is retaken when a target moved during evaluation; if it keeps
        moving the last view is returned and :meth:`DeliveryView.fresh` stays
        false for the caller to fail closed on.  Only a read-only surface
        passes *max_age* (see :data:`READ_MAX_AGE`); every guarded writer
        fetches. ``cached_only`` is for interactive diagnostics: absent or
        expired snapshots return unknown without fetching or waiting on Git.
        """
        ids = set(task_ids)
        target_loader = target_loader or self.target_loader
        view = DeliveryView(self.db)
        if not ids:
            return view
        for _attempt in range(self.FRESH_ATTEMPTS):
            async with self.db._engine.connect() as conn:
                targets = await target_loader(conn, ids)
            groups: dict[DeliveryTarget, set[str]] = {}
            for task_id, target in targets.items():
                groups.setdefault(target, set()).add(task_id)
            evidence: dict[str, DeliveryEvidence] = {}
            snapshots = []
            repositories = {}
            for target, group in sorted(groups.items(), key=lambda item: item[0].repository_id):
                shared = None
                if self.truth is not None and not cached_only:
                    from src.integration.git_truth import GitTruthSnapshot

                    key = target.project_id, target.repository_id, target.repository_url
                    if key not in repositories:
                        repositories[key] = GitTruthSnapshot(
                            self.truth, await self._snapshot(
                                target, max_age, cached_only=cached_only,
                            ),
                        )
                    shared = repositories[key].for_target(target.target_ref).observation
                snapshot, found = await self._evaluate(
                    target, group, max_age, snapshot=shared, cached_only=cached_only,
                )
                snapshots.append(snapshot)
                evidence.update(found)
            view = DeliveryView(self.db, evidence, targets, tuple(snapshots), self.request_loader,
                                target_loader)
            if cached_only or max_age > 0 or await view.fresh():
                return view
        return view
