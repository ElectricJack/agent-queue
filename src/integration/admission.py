"""Request-scoped development admission; git is observed outside row locks.

The DB projection answers graph/gate readiness only. This batch gathers the
completed prerequisites of structurally ready candidates, observes one fetched
snapshot per repository, and supplies an ephemeral allowed set to consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from src.database.queries.blocked_state import blocked_predicate
from src.database.tables import (
    archived_tasks,
    gates,
    projects,
    repos,
    task_completion_records,
    task_dependencies,
    task_gates,
    task_labels,
    task_metadata,
    tasks,
)
from src.git.manager import GitError
from src.integration.delivery_truth import DeliveryRequest, delivery_snapshot
from src.integration.publishable_artifact import legacy_artifact


async def _inputs(db, candidate_ids, conn):
    """Read graph inputs and immutable completion identities on one connection."""
    candidate_ids = set(candidate_ids)
    candidates = (
        (
            await conn.execute(
                select(
                    tasks.c.id,
                    tasks.c.project_id,
                ).where(tasks.c.id.in_(candidate_ids))
            )
        )
        .mappings()
        .all()
    )
    project_ids = {r["project_id"] for r in candidates}
    project_rows = (
        (await conn.execute(select(projects).where(projects.c.id.in_(project_ids))))
        .mappings()
        .all()
    )
    project_by_id = {r["id"]: dict(r) for r in project_rows}
    dev_ids = {
        r["id"]
        for r in candidates
        if project_by_id[r["project_id"]]["hierarchical_integration_mode"] == "development"
    }
    edges = (
        (
            await conn.execute(
                select(task_dependencies).where(task_dependencies.c.task_id.in_(dev_ids))
            )
        )
        .mappings()
        .all()
    )
    # waits-for consumes a container's children, rather than its own artifact.
    fan_in = {r["depends_on_task_id"] for r in edges if r["dep_type"] == "waits-for"}
    children = (
        (
            await conn.execute(
                select(task_dependencies).where(
                    task_dependencies.c.depends_on_task_id.in_(fan_in),
                    task_dependencies.c.dep_type == "parent-child",
                )
            )
        )
        .mappings()
        .all()
        if fan_in
        else []
    )
    required = {tid: set() for tid in dev_ids}
    for edge in edges:
        if edge["dep_type"] == "blocks":
            required[edge["task_id"]].add(edge["depends_on_task_id"])
        elif edge["dep_type"] == "waits-for":
            required[edge["task_id"]].update(
                child["task_id"]
                for child in children
                if child["depends_on_task_id"] == edge["depends_on_task_id"]
            )
    source_ids = set().union(*required.values()) if required else set()
    source_rows = (
        (await conn.execute(select(tasks).where(tasks.c.id.in_(source_ids)))).mappings().all()
        if source_ids
        else []
    )
    source_rows = [{**row, "archived": False} for row in source_rows]
    missing = source_ids - {row["id"] for row in source_rows}
    if missing:
        archived = (
            (await conn.execute(select(archived_tasks).where(archived_tasks.c.id.in_(missing))))
            .mappings()
            .all()
        )
        source_rows.extend({**row, "archived": True} for row in archived)
    completions = (
        (
            await conn.execute(
                select(task_completion_records)
                .where(task_completion_records.c.task_id.in_(source_ids))
                .distinct(task_completion_records.c.task_id)
                .order_by(
                    task_completion_records.c.task_id,
                    task_completion_records.c.completed_at.desc(),
                    task_completion_records.c.id.desc(),
                )
            )
        )
        .mappings()
        .all()
        if source_ids
        else []
    )
    completion_by_id = {r["task_id"]: db._row_to_task_completion(r) for r in completions}
    recorded = (
        set(
            (
                await conn.execute(
                    select(task_completion_records.c.task_id).where(
                        task_completion_records.c.task_id.in_(source_ids),
                        task_completion_records.c.commits != "[]",
                    )
                )
            ).scalars()
        )
        # A retired manifest's unresolved source keeps the generation unknown.
        | set(
            (
                await conn.execute(
                    select(tasks.c.id).where(tasks.c.id.in_(source_ids), legacy_artifact(tasks))
                )
            ).scalars()
        )
        if source_ids
        else set()
    )
    # Obsolete is operation intent, not a delivered projection.
    markers = (
        (
            await conn.execute(
                select(task_metadata).where(
                    task_metadata.c.task_id.in_(source_ids),
                    task_metadata.c.key == "obsolete",
                )
            )
        )
        .mappings()
        .all()
        if source_ids
        else []
    )
    obsolete = {r["task_id"] for r in markers}
    gate_rows = (
        (
            await conn.execute(
                select(task_gates, gates.c.status)
                .join(gates, gates.c.id == task_gates.c.gate_id)
                .where(task_gates.c.task_id.in_(dev_ids))
            )
        )
        .mappings()
        .all()
    )
    labels = (
        (await conn.execute(select(task_labels).where(task_labels.c.task_id.in_(dev_ids))))
        .mappings()
        .all()
    )
    repo_ids = {
        r["integration_repository_id"]
        for r in project_rows
        if r["hierarchical_integration_mode"] == "development"
    } - {None}
    repo_rows = (
        (await conn.execute(select(repos).where(repos.c.id.in_(repo_ids)))).mappings().all()
        if repo_ids
        else []
    )
    repo_by_id = {r["id"]: dict(r) for r in repo_rows}
    requests = {}
    for row in source_rows:
        project = project_by_id.get(row["project_id"])
        # A cross-project source must be scoped to its own integration config.
        if project is None:
            project_row = (
                (await conn.execute(select(projects).where(projects.c.id == row["project_id"])))
                .mappings()
                .one_or_none()
            )
            if project_row:
                project = project_by_id[row["project_id"]] = dict(project_row)
        if not project or project["hierarchical_integration_mode"] != "development":
            continue
        repo_id = project["integration_repository_id"]
        if repo_id not in repo_by_id and repo_id:
            repo = (
                (await conn.execute(select(repos).where(repos.c.id == repo_id)))
                .mappings()
                .one_or_none()
            )
            if repo:
                repo_by_id[repo_id] = dict(repo)
        repo = repo_by_id.get(repo_id)
        requests[row["id"]] = replace(
            DeliveryRequest.from_task(
                row,
                completion_by_id.get(row["id"]),
                repository_id=repo_id or "",
                target_ref="refs/heads/" + repo["default_branch"] if repo else "",
            ),
            has_recorded_source=row["id"] in recorded,
        )

    # Compare values, not counts: a replacement edge/gate is movement too.
    def frozen(rows):
        return tuple(sorted(repr(sorted(dict(row).items())) for row in rows))

    fingerprint = (
        frozen(edges),
        frozen(children),
        frozen(gate_rows),
        frozen(markers),
        tuple(sorted(requests.items())),
        tuple(
            sorted(
                (
                    pid,
                    p["hierarchical_integration_mode"],
                    p["integration_repository_id"],
                    p["hierarchical_integration_generation"],
                )
                for pid, p in project_by_id.items()
            )
        ),
        tuple(
            sorted(
                (rid, r["project_id"], r["url"], r["default_branch"])
                for rid, r in repo_by_id.items()
            )
        ),
    )
    fingerprint += (frozen(labels),)
    return dev_ids, required, requests, obsolete, repo_by_id, fingerprint


@dataclass
class AdmissionSnapshot:
    db: object
    candidate_ids: set[str]
    allowed: set[str]
    inputs: tuple
    snapshots: list = field(default_factory=list)
    reasons: dict[str, list[dict]] = field(default_factory=dict)
    lock_ids: set[str] = field(default_factory=set)
    required: dict[str, set[str]] = field(default_factory=dict)
    changed: bool = False

    async def is_fresh(self, task_id=None):
        required = self.required.get(task_id, set()) if task_id else None
        return all(
            [
                await snapshot.is_fresh()
                for snapshot in self.snapshots
                if required is None or required.intersection(snapshot._identities)
            ]
        )

    async def matches(self, *, conn=None, lock=False, task_id=None):
        """Fence graph/generation/config under the mutation's transaction.

        Task input locks serialize completion/reopen and the graph projection
        writers. NOWAIT avoids deadlocking a claimer with a graph writer that
        holds a prerequisite and is waiting for the candidate. Contention is
        movement and retries. There is no network I/O inside this guard.
        """
        if conn is None:
            async with self.db._engine.connect() as owned:
                return await self.matches(conn=owned)
        if lock and (self.lock_ids or task_id):
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        select(tasks.c.id)
                        .where(tasks.c.id.in_(self.lock_ids | ({task_id} if task_id else set())))
                        .order_by(tasks.c.id)
                        .with_for_update(read=True, nowait=True)
                    )
                    # Freeze integration configuration until activation commits.
                    await conn.execute(
                        select(projects.c.id)
                        .where(projects.c.id.in_([p[0] for p in self.inputs[5]]))
                        .order_by(projects.c.id)
                        .with_for_update(read=True, nowait=True)
                    )
                    await conn.execute(
                        select(repos.c.id)
                        .where(repos.c.id.in_([r[0] for r in self.inputs[6]]))
                        .order_by(repos.c.id)
                        .with_for_update(read=True, nowait=True)
                    )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "55P03":
                    return False
                raise
        current = await _inputs(self.db, self.candidate_ids, conn)
        return self.inputs == current[-1]


async def observe_admission(db, candidate_ids, service):
    """One batch for readiness, pool demand, explanation and actual claims."""
    candidate_ids = set(candidate_ids)
    async with db._engine.connect() as conn:
        dev_ids, required, requests, obsolete, repo_rows, inputs = await _inputs(
            db, candidate_ids, conn
        )
    batch = AdmissionSnapshot(db, candidate_ids, candidate_ids - dev_ids, inputs)
    batch.lock_ids = set(requests)
    batch.required = required
    evidence = {}
    scopes = {
        (r.project_id, r.repository_id, r.target_ref)
        for r in requests.values()
        if r.task_status == "COMPLETED" and r.task_id not in obsolete
    }
    for project_id, repo_id, target in sorted(scopes):
        repo = repo_rows.get(repo_id)
        if not repo or repo["project_id"] != project_id:
            continue  # Missing/wrong configuration withholds the prerequisite.
        try:
            store = await service.store(db._row_to_repo(repo), fetch=False)
            snapshot = await delivery_snapshot(
                service.git,
                store,
                project_id=project_id,
                repository_id=repo_id,
                repository_url=repo["url"],
                target_ref=target,
            )
            batch.snapshots.append(snapshot)
            scoped = [
                r
                for r in requests.values()
                if (r.project_id, r.repository_id, r.target_ref) == (project_id, repo_id, target)
            ]
            observed = await snapshot.evaluate_many(scoped)
            evidence.update(observed)
        except (GitError, OSError, ValueError):
            continue  # Diagnostic unknown; never treat failed git as no artifact.
    for task_id, deps in required.items():
        reasons = []
        for dep_id in sorted(deps):
            request = requests.get(dep_id)
            if dep_id in obsolete or request is None:
                continue  # Non-development dependencies use the graph predicate.
            proof = evidence.get(dep_id)
            if request.task_status != "COMPLETED" or proof is None or not proof.satisfied:
                reasons.append(
                    {
                        "code": "development_dependency_delivery",
                        "ref": dep_id,
                        "detail": f"Development prerequisite {dep_id}: "
                        + (f"{proof.state} ({proof.reason})" if proof else "unknown"),
                    }
                )
        if reasons:
            batch.reasons[task_id] = reasons
        else:
            batch.allowed.add(task_id)
    # A moving target/generation never produces a usable allowed set. Callers
    # retry snapshots; they never write this answer back into the database.
    for snapshot in batch.snapshots:
        if not await snapshot.is_fresh():
            batch.changed |= snapshot.error is None
            for task_id, deps in required.items():
                if deps.intersection(snapshot._identities):
                    batch.allowed.discard(task_id)
                    batch.reasons.setdefault(task_id, []).append(
                        {
                            "code": "development_snapshot_changed",
                            "ref": task_id,
                            "detail": "Development target is unknown or moved; admission will retry",
                        }
                    )
    if not await batch.matches():
        batch.changed = True
        batch.allowed -= dev_ids
    return batch


async def structural_candidates(db, project_id, *, page_size=128):
    """Keyset page past withheld candidates in the existing fairness order."""
    from sqlalchemy import tuple_

    from src.database.queries.blocked_state import apply_label_filters
    from src.database.queries.claim_queries import _frontier_where

    ids, cursor = [], None
    async with db._engine.connect() as conn:
        while True:
            stmt = apply_label_filters(
                select(
                    tasks.c.id,
                    tasks.c.priority,
                    tasks.c.created_at,
                ).where(_frontier_where(project_id), ~blocked_predicate()),
                exclude_hold=True,
            )
            if cursor:
                stmt = stmt.where(tuple_(tasks.c.priority, tasks.c.created_at, tasks.c.id) > cursor)
            rows = (
                await conn.execute(
                    stmt.order_by(
                        tasks.c.priority,
                        tasks.c.created_at,
                        tasks.c.id,
                    ).limit(page_size)
                )
            ).all()
            ids.extend(row.id for row in rows)
            if len(rows) < page_size:
                return ids
            last = rows[-1]
            cursor = (last.priority, last.created_at, last.id)
