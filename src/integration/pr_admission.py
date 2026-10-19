"""Current PR sources shared by readiness, pool demand and claim activation.

Completion gates are a useful visible wait, but cannot authorize a later edge,
replacement PR or a stacked checkout. Git containment of the current PR source
is the authority. Only links inserted by completion bookkeeping may be removed;
explicit gates are never resolved or waived here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from src.database.tables import (
    projects,
    repos,
    task_completion_records,
    task_dependencies,
    task_gates,
    task_labels,
    task_metadata,
    tasks,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.delivery_observer import DeliveryTarget
from src.integration.delivery_path import MANAGED_MODES, local_repository_path
from src.models import resolve_integration_mode_with_source


async def _inputs(db, ids, default_mode, conn):
    edges = (
        (
            await conn.execute(
                select(task_dependencies).where(
                    task_dependencies.c.task_id.in_(ids),
                    task_dependencies.c.dep_type == "blocks",
                )
            )
        )
        .mappings()
        .all()
    )
    empty_fence = {"tasks": set(), "projects": set(), "repos": set(), "generated": set()}
    if not edges:
        # A newly inserted blocks edge changes this sentinel on revalidation.
        # Ordinary claims need no PR configuration or remote observation.
        return {}, {}, {}, empty_fence, ("no-blocks",)
    source_ids = {e["depends_on_task_id"] for e in edges}
    rows = (
        (
            await conn.execute(
                select(tasks).where(
                    tasks.c.id.in_(source_ids | set(ids)),
                )
            )
        )
        .mappings()
        .all()
    )
    by_id = {r["id"]: dict(r) for r in rows}
    project_rows = (
        (
            await conn.execute(
                select(projects).where(
                    projects.c.id.in_({r["project_id"] for r in rows}),
                )
            )
        )
        .mappings()
        .all()
    )
    project_by_id = {r["id"]: dict(r) for r in project_rows}
    if all(
        project_by_id[row["project_id"]]["hierarchical_integration_mode"] in MANAGED_MODES
        for row in rows
    ):
        # Managed preparation owns its graph/source changes. Preserve those
        # existing fences; an incoming non-managed source changes this shape
        # and enters the PR guard on revalidation.
        fence = {**empty_fence, "projects": set(project_by_id)}
        modes = tuple(
            sorted((pid, p["hierarchical_integration_mode"]) for pid, p in project_by_id.items())
        )
        return {}, {}, by_id, fence, ("managed", modes)
    repo_rows = (
        (
            await conn.execute(
                select(repos).where(
                    repos.c.project_id.in_(project_by_id),
                )
            )
        )
        .mappings()
        .all()
    )
    repo_by_id = {r["id"]: dict(r) for r in repo_rows}
    parents = {r["parent_task_id"] for r in rows if r["is_plan_subtask"] and r["parent_task_id"]}
    parent_modes = (
        dict(
            (
                await conn.execute(
                    select(tasks.c.id, tasks.c.integration_mode).where(
                        tasks.c.id.in_(parents),
                    )
                )
            ).all()
        )
        if parents
        else {}
    )
    completions = (
        (
            await conn.execute(
                select(task_completion_records)
                .where(
                    task_completion_records.c.task_id.in_(source_ids),
                )
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
    completed = {r["task_id"]: db._row_to_task_completion(r) for r in completions}
    recorded_sources = set(
        (
            await conn.execute(
                select(task_completion_records.c.task_id).where(
                    task_completion_records.c.task_id.in_(source_ids),
                    task_completion_records.c.commits != "[]",
                )
            )
        ).scalars()
    )

    def repository(row):
        project = project_by_id[row["project_id"]]
        if row["repo_id"]:
            return repo_by_id.get(row["repo_id"])
        own = [r for r in repo_rows if r["project_id"] == project["id"]]
        selected = repo_by_id.get(project["integration_repository_id"])
        if selected and selected["project_id"] == project["id"]:
            return selected
        if len(own) == 1:
            return own[0]
        # Ordinary projects embed their repository on the project row. The
        # observer's repository id is a scope key, not a database foreign key.
        if not own and project["repo_url"] and project["repo_default_branch"]:
            return {
                "id": "project:" + project["id"],
                "project_id": project["id"],
                "url": project["repo_url"],
                "default_branch": project["repo_default_branch"],
            }
        return None

    for row in by_id.values():
        selected = repository(row)
        row["effective_repository_id"] = selected["id"] if selected else None
    sources = {}
    for source_id in sorted(source_ids):
        row = by_id.get(source_id)
        if row is None:
            continue  # A deleted edge target is handled by the graph writer.
        project = project_by_id[row["project_id"]]
        if project["hierarchical_integration_mode"] in MANAGED_MODES:
            continue
        repo = repository(row)
        url = repo["url"] if repo else project["repo_url"]
        mode, _ = resolve_integration_mode_with_source(
            row["integration_mode"],
            parent_task_mode=parent_modes.get(row["parent_task_id"]),
            project_mode=project["integration_mode"],
            default_mode=default_mode,
            pull_requests_available=local_repository_path(url) is None,
        )
        has_artifact = row["branch_name"] or row["pr_url"] or source_id in recorded_sources
        if mode == "pull_request" and has_artifact:
            sources[source_id] = (row, repo, url, completed.get(source_id))
    required = {
        tid: {
            e["depends_on_task_id"]
            for e in edges
            if e["task_id"] == tid and e["depends_on_task_id"] in sources
        }
        for tid in ids
    }
    # Omit candidate lifecycle/updated_at: taking the claim changes those before
    # activation. Non-completed candidate sources cannot authorize any dependent
    # in this batch, so their lifecycle is omitted from the source fence too.
    # Their selected branch, repository and policy remain fenced.
    candidates = [
        (tid, by_id[tid]["repo_id"], by_id[tid]["branch_name"])
        for tid in sorted(ids)
        if tid in by_id
    ]

    def freeze(items):
        return tuple(sorted(repr(sorted(dict(row).items())) for row in items))

    gates = (
        (await conn.execute(select(task_gates).where(task_gates.c.task_id.in_(ids))))
        .mappings()
        .all()
    )
    labels = (
        (await conn.execute(select(task_labels).where(task_labels.c.task_id.in_(ids))))
        .mappings()
        .all()
    )
    generated = (
        (
            await conn.execute(
                select(task_metadata).where(
                    task_metadata.c.task_id.in_(ids),
                    task_metadata.c.key.startswith("pr_dependency_gate:", autoescape=True),
                )
            )
        )
        .mappings()
        .all()
    )
    fingerprint = (
        freeze(edges),
        freeze([by_id[s] for s in sources if s not in ids or by_id[s]["status"] == "COMPLETED"]),
        freeze(project_rows),
        freeze(repo_rows),
        freeze(completions),
        tuple(sorted(recorded_sources)),
        tuple(candidates),
        tuple(sorted(parent_modes.items())),
        default_mode,
        freeze(gates),
        freeze(labels),
        freeze(generated),
    )
    fence = {
        "tasks": (set(by_id) - set(ids)) | parents,
        "projects": set(project_by_id),
        "repos": set(repo_by_id),
        "generated": {row["task_id"] for row in generated},
    }
    return sources, required, by_id, fence, fingerprint


@dataclass
class PRAdmission:
    db: object
    service: object
    ids: set
    default_mode: str
    inputs: tuple
    allowed: set = field(default_factory=set)
    reasons: dict = field(default_factory=dict)
    snapshots: list = field(default_factory=list)
    observations: list = field(default_factory=list)
    workspace_sources: dict = field(default_factory=dict)
    stacked_bases: dict = field(default_factory=dict)
    lock_ids: set = field(default_factory=set)
    source_projects: set = field(default_factory=set)
    source_repos: set = field(default_factory=set)
    changed: bool = False

    async def matches(self, *, conn, lock=False, task_id=None):
        if lock:
            # The outer AdmissionSnapshot locks candidates and all edge sources,
            # including sources whose policy could switch to PR during a claim.
            try:
                async with conn.begin_nested():
                    await conn.execute(
                        select(projects.c.id)
                        .where(
                            projects.c.id.in_(self.source_projects),
                        )
                        .order_by(projects.c.id)
                        .with_for_update(read=True, nowait=True)
                    )
                    await conn.execute(
                        select(repos.c.id)
                        .where(
                            repos.c.id.in_(self.source_repos),
                        )
                        .order_by(repos.c.id)
                        .with_for_update(read=True, nowait=True)
                    )
            except DBAPIError as exc:
                if getattr(exc.orig, "sqlstate", None) == "55P03":
                    return False
                raise
        return self.inputs == (await _inputs(self.db, self.ids, self.default_mode, conn))[-1]

    async def is_fresh(self, task_id=None):
        try:
            for client, url, identity in self.observations:
                if _identity(await client.pull_request(url)) != identity:
                    return False
            return all([await snapshot.is_fresh() for snapshot in self.snapshots])
        except Exception:  # Remote unavailability never establishes delivery.
            return False

    async def verify_workspace(self, task_id, path):
        try:
            return all(
                [
                    await self.service.git.ais_ancestor(path, oid, "HEAD")
                    for oid in self.workspace_sources.get(task_id, ())
                ]
            )
        except (GitError, OSError):
            return False


def _identity(pull):
    return (
        pull.get("state"),
        bool(pull.get("merged")),
        pull.get("head", {}).get("sha"),
        pull.get("head", {}).get("ref"),
        pull.get("base", {}).get("ref"),
        pull.get("merge_commit_sha"),
        pull.get("base", {}).get("repo", {}).get("full_name"),
    )


async def observe_pr_admission(db, ids, service, *, default_mode, cached_only=False):
    async with db._engine.connect() as conn:
        sources, required, candidates, fence, inputs = await _inputs(db, ids, default_mode, conn)
    batch = PRAdmission(db, service, ids, default_mode, inputs, allowed=set(ids))
    batch.lock_ids = fence["tasks"]
    batch.source_projects = fence["projects"]
    batch.source_repos = fence["repos"]
    proofs = {}
    for source_id, (row, repo, url, completion) in sources.items():
        proof = None
        try:
            # Interactive reads never initiate remote work or bless unknown proof.
            if cached_only or row["status"] != "COMPLETED" or not repo or not row["pr_url"]:
                raise ValueError("current source unavailable")
            binding = await service.git.bind_github_repository(url)
            from src.git.github import GitHubAccess

            GitHubAccess.validate_pr_url(binding, row["pr_url"])
            client = service.git._github_client(binding)
            pull = await client.pull_request(row["pr_url"])
            state, merged, head, branch, base, merge_oid, repository = _identity(pull)
            if (
                not is_valid_git_oid(head)
                or state not in {"open", "closed"}
                or branch != row["branch_name"]
                or repository != binding.full_name
                or not base
                or state == "closed"
                and not merged
                or completion
                and completion.commits
                and completion.commits[-1] != head
            ):
                raise ValueError("PR does not identify the current completed source")
            snapshot = await service.delivery_observer.snapshot(
                DeliveryTarget(
                    row["project_id"],
                    repo["id"],
                    url,
                    "refs/heads/" + repo["default_branch"],
                )
            )
            if snapshot.error or not snapshot.target_oid:
                raise ValueError("default source unavailable")
            batch.snapshots.append(snapshot)
            batch.observations.append((client, row["pr_url"], _identity(pull)))
            # A squash merge carries the merge object, rather than head ancestry.
            oid = merge_oid if merged and is_valid_git_oid(merge_oid) else head
            observation = getattr(snapshot, "observation", snapshot)
            source_ref = "refs/remotes/origin/" + branch
            source_head = observation.source_heads.get(source_ref)
            if source_head != head and (source_head is not None or not merged):
                raise ValueError("repository source branch no longer matches the PR head")
            if source_head is not None:
                batch.snapshots.append(
                    replace(
                        observation,
                        target_ref="refs/heads/" + branch,
                        target_oid=head,
                    )
                )
            contained = await service.git.ais_ancestor(
                observation.store,
                oid,
                snapshot.target_oid,
                strict=True,
            )
            proof = (head, oid, contained is True, observation, repo)
        except Exception:  # Malformed, unavailable or foreign evidence fails closed.
            pass
        proofs[source_id] = proof
    for task_id, deps in required.items():
        for source_id in sorted(deps):
            proof = proofs[source_id]
            satisfied = False
            if proof:
                head, oid, contained, snapshot, repo = proof
                candidate = candidates.get(task_id, {})
                same_repository = candidate.get("effective_repository_id") == repo["id"]
                if contained:
                    satisfied = True
                    if same_repository:
                        batch.workspace_sources.setdefault(task_id, []).append(oid)
                elif same_repository and candidate.get("branch_name"):
                    ref = "refs/remotes/origin/" + candidate["branch_name"]
                    tip = snapshot.source_heads.get(ref)
                    if is_valid_git_oid(tip) and await service.git.ais_ancestor(
                        snapshot.store, head, tip
                    ):
                        satisfied = True
                        batch.workspace_sources.setdefault(task_id, []).append(head)
                        batch.stacked_bases[task_id] = (
                            snapshot.store,
                            tip,
                            candidate["branch_name"],
                        )
                        # Pin the stacked remote too; default freshness alone cannot
                        # guard a rewound dependent branch during preparation.
                        batch.snapshots.append(
                            replace(
                                snapshot,
                                target_ref="refs/heads/" + candidate["branch_name"],
                                target_oid=tip,
                            )
                        )
            if not satisfied:
                batch.allowed.discard(task_id)
                batch.reasons.setdefault(task_id, []).append(
                    {
                        "code": "pull_request_dependency_delivery",
                        "ref": source_id,
                        "detail": f"PR prerequisite {source_id} lacks current source containment "
                        "on the default branch or the dependent's exact workspace base",
                    }
                )
    if not cached_only and not await batch.is_fresh():
        batch.allowed -= {tid for tid, deps in required.items() if deps}
        batch.changed = True
    # Gate bookkeeping is subordinate to the same proof. Remove only generated
    # associations, never resolve the shared gate (which may have human waiters).
    if not cached_only and batch.allowed.intersection(fence["generated"]):
        try:
            async with db.immediate() as conn:
                # Writers lock inputs before retiring links. Contention is
                # movement, rather than a wait while holding another source.
                await conn.execute(
                    select(tasks.c.id)
                    .where(
                        tasks.c.id.in_(batch.lock_ids | ids),
                    )
                    .order_by(tasks.c.id)
                    .with_for_update(nowait=True)
                )
                if not await batch.matches(conn=conn, lock=True):
                    batch.allowed.clear()
                    batch.changed = True
                else:
                    flipped = await db.release_pr_dependency_gates(conn, batch.allowed)
                    batch.inputs = (await _inputs(db, ids, default_mode, conn))[-1]
            if not batch.changed:
                await db.log_blocked_flips(flipped)
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            batch.allowed.clear()
            batch.changed = True
    return batch
