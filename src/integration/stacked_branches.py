"""Git-proven prerequisite stacks, shared by frontier and delivery admission.

A stack is source provenance, never proof of delivery. The immutable filing base
remains intact. An idle completed child can merge a new prerequisite incarnation;
a live writer or an unresolved prerequisite withholds delivery instead.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field, replace
from types import SimpleNamespace

from sqlalchemy import exists, insert, literal, or_, select, update

from src.database.tables import (
    projects,
    repos,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_dependencies,
    task_integration_checkpoints,
    task_metadata,
    tasks,
)
from src.git.manager import GitError, RemoteRefState, is_valid_git_oid
from src.integration.batches import Batch, BatchMember, BatchStore
from src.integration.delivery_observer import DeliveryTarget, prerequisite_observer
from src.integration.train import TrainTarget


def stacked_policy(project) -> bool:
    """Explicit project choice wins; agent-queue opts in by default."""

    def value(key, default=None):
        return (
            project.get(key, default)
            if isinstance(project, dict)
            else getattr(project, key, default)
        )

    policy = value("hierarchical_integration_policy") or {}
    choice = policy.get("prerequisite_branches")
    return choice == "stacked" or choice is None and value("id") == "agent-queue"


async def _inputs(conn, project_id, task_id=None):
    dependent, source = tasks.alias("stack_child"), tasks.alias("stack_source")
    checkpoint = task_integration_checkpoints
    query = (
        select(
            dependent.c.id.label("dependent"),
            source.c.id.label("task_id"),
            source.c.parent_task_id,
            source.c.repo_id,
            source.c.branch_name,
            source.c.status,
            source.c.claim_epoch,
            source.c.updated_at,
            exists(
                select(literal(1)).where(
                    task_metadata.c.task_id == source.c.id,
                    or_(
                        task_metadata.c.key == "obsolete",
                        (task_metadata.c.key == "work_outcome")
                        & (task_metadata.c.value == json.dumps("abandoned")),
                    ),
                )
            ).label("abandoned"),
            checkpoint.c.checkpoint_sha,
            checkpoint.c.generation,
            checkpoint.c.version,
            checkpoint.c.repository_id,
            checkpoint.c.branch,
        )
        .select_from(
            task_dependencies.join(
                dependent,
                dependent.c.id == task_dependencies.c.task_id,
            )
            .join(source, source.c.id == task_dependencies.c.depends_on_task_id)
            .outerjoin(
                checkpoint,
                checkpoint.c.task_id == source.c.id,
            )
        )
        .where(
            dependent.c.project_id == project_id,
            dependent.c.parent_task_id.is_not(None),
            source.c.project_id == project_id,
            source.c.parent_task_id == dependent.c.parent_task_id,
            task_dependencies.c.dep_type == "blocks",
        )
    )
    if task_id is not None:
        query = query.where(dependent.c.id == task_id)
    return tuple(dict(row) for row in (await conn.execute(query)).mappings())


def _identity(row):
    return {key: value for key, value in row.items() if key != "dependent"}


@dataclass
class StackView:
    db: object
    project_id: str
    rows: tuple[dict, ...] = ()
    proofs: dict[str, dict] = field(default_factory=dict)
    delivered_refs: dict[str, tuple[str, str]] = field(default_factory=dict)
    watched_refs: dict[str, str | None] = field(default_factory=dict)
    snapshot: object = None
    ref_cache: dict = field(default_factory=dict, repr=False)
    fresh_max_age: float = 0
    cached_only: bool = False

    async def fresh(self):
        if self.snapshot is None:
            return not self.rows
        snapshot = self.snapshot
        if snapshot.error or not snapshot.target_oid:
            return False
        target = snapshot.target_ref.removeprefix("refs/heads/")
        branches = {target}
        branches.update(proof["branch_name"].removeprefix("refs/heads/")
                        for proof in self.proofs.values())
        branches.update(branch for branch, _ in self.delivered_refs.values())
        branches.update(self.watched_refs)
        now = time.monotonic()
        key = str(snapshot.store), snapshot.repository_url
        refs = {}
        missing = []
        for branch in sorted(branches):
            cached = self.ref_cache.get((*key, branch))
            if self.fresh_max_age > 0 and cached and now - cached[0] < self.fresh_max_age:
                refs[branch] = cached[1]
            else:
                missing.append(branch)
        if self.cached_only and missing:
            return False
        try:
            if await snapshot.git.aget_remote_url(snapshot.store) != snapshot.repository_url:
                return False
            observed = await snapshot.git.als_remote_refs(snapshot.store, missing) if missing else {}
        except (GitError, OSError):
            return False
        for branch, ref in observed.items():
            refs[branch] = ref
            self.ref_cache[(*key, branch)] = now, ref
        # Expire old entries even when the project keeps changing its task refs.
        for cached_key, (stamp, _) in list(self.ref_cache.items()):
            if now - stamp > 2:
                self.ref_cache.pop(cached_key, None)
        ref = refs.get(target)
        if ref is None or ref.state is not RemoteRefState.PRESENT or ref.oid != snapshot.target_oid:
            return False
        for branch, expected in self.watched_refs.items():
            ref = refs.get(branch)
            if ref is None or (ref.state, ref.oid) != (
                RemoteRefState.PRESENT if expected else RemoteRefState.ABSENT, expected,
            ):
                return False
        for proof in self.proofs.values():
            ref = refs.get(proof["branch_name"].removeprefix("refs/heads/"))
            if ref is None:
                return False
            if ref.state is RemoteRefState.PRESENT and ref.oid == proof["checkpoint_sha"]:
                continue
            delivered = self.delivered_refs.get(proof["task_id"])
            if ref.state is not RemoteRefState.ABSENT or delivered is None:
                return False
            parent = refs.get(delivered[0])
            if parent is None or parent.state is not RemoteRefState.PRESENT or parent.oid != delivered[1]:
                return False
        return True

    async def verified_on(self, conn, ids=None):
        current = {
            _identity(row)["task_id"]: _identity(row)
            for row in await _inputs(conn, self.project_id)
        }
        return {
            tid: proof
            for tid, proof in self.proofs.items()
            if (ids is None or tid in ids) and current.get(tid) == proof
        }

    def required(self, task_id):
        return {row["task_id"] for row in self.rows if row["dependent"] == task_id}


async def observe_stacks(observer, project_id, *, task_id=None, max_age=0, snapshot=None,
                         cached_only=False):
    async with observer.db._engine.connect() as conn:
        rows = await _inputs(conn, project_id, task_id)
        parents = dict(
            (
                await conn.execute(
                    select(tasks.c.id, tasks.c.branch_name).where(
                        tasks.c.id.in_({row["parent_task_id"] for row in rows})
                    )
                )
            ).all()
        )
        repository = (
            (
                await conn.execute(
                    select(repos)
                    .join(
                        projects,
                        projects.c.integration_repository_id == repos.c.id,
                    )
                    .where(projects.c.id == project_id)
                )
            )
            .mappings()
            .one_or_none()
        )
    view = StackView(observer.db, project_id, rows,
                     ref_cache=observer._stack_ref_cache, fresh_max_age=min(max_age, 2),
                     cached_only=cached_only)
    if not rows or repository is None:
        return view
    target = DeliveryTarget(
        project_id,
        repository["id"],
        repository["url"],
        "refs/heads/" + repository["default_branch"],
    )
    view.snapshot = snapshot or await observer._snapshot(target, max_age, cached_only=cached_only)
    if (view.snapshot.project_id, view.snapshot.repository_id, view.snapshot.repository_url) != (
        project_id, repository["id"], repository["url"],
    ):
        raise GitError("stack observation belongs to another repository")
    if view.snapshot.error:
        return view
    for row in rows:
        proof = _identity(row)
        head, ref = row["checkpoint_sha"], row["branch_name"]
        if (
            row["status"] != "COMPLETED"
            or row["abandoned"]
            or not is_valid_git_oid(head or "")
            or not ref
            or row["repository_id"] != repository["id"]
            or row["repo_id"] != repository["id"]
            or row["branch"] != ref
        ):
            continue
        remote = "refs/remotes/origin/" + ref.removeprefix("refs/heads/")
        remote_head = view.snapshot.source_heads.get(remote)
        if remote_head != head:
            # Cleaned source refs remain usable only with current completion delivery proof.
            parent = parents.get(row["parent_task_id"])
            if remote_head is not None or not parent or observer.truth is None:
                continue
            from src.integration.delivery_truth import DeliveryState, load_delivery_requests
            from src.integration.git_truth import GitTruthSnapshot

            parent_ref = "refs/heads/" + parent.removeprefix("refs/heads/")
            delivered = GitTruthSnapshot(observer.truth, view.snapshot).for_target(parent_ref)
            requests = await load_delivery_requests(
                observer.db,
                (row["task_id"],),
                repository_id=repository["id"],
                target_ref=parent_ref,
                reduced=True,
            )
            request = requests.get(row["task_id"])
            if request is None:
                continue
            evidence = await delivered.is_delivered(request)
            if evidence.state is not DeliveryState.CONTAINED or evidence.source_oid != head:
                continue
            view.delivered_refs[row["task_id"]] = (
                parent_ref.removeprefix("refs/heads/"),
                delivered.target_oid,
            )
        exact = await observer.git.arev_parse(view.snapshot.store, head + "^{commit}")
        if exact == head:
            view.proofs[row["task_id"]] = proof
    return view


async def merge_heads(git, store, base, heads, *, stamp):
    """Merge immutable objects without changing a checkout; pin the result locally."""
    from src.git.manager import commit_identity
    from src.integration.regeneration import DEFAULT_REGENERATE_COMMAND, merge_generated_tree

    current = base
    for head in heads:
        result = await git.arun_git_result(
            ["--no-replace-objects", "merge-base", "--is-ancestor", head, current],
            cwd=str(store),
        )
        if result.returncode == 0:
            continue
        if result.returncode != 1:
            raise GitError("stack ancestry is unavailable")
        with commit_identity(git.resolve_commit_identity()):
            tree = await merge_generated_tree(
                git,
                store,
                ["--no-replace-objects", "merge-tree", "--write-tree", current, head],
                command=DEFAULT_REGENERATE_COMMAND,
                timeout_seconds=600,
            )
        result = await git.arun_git_result(
            [
                *git.resolve_commit_identity().config_args(),
                "commit-tree",
                tree,
                "-p",
                current,
                "-p",
                head,
                "-m",
                "Merge prerequisite stack",
            ],
            cwd=str(store),
            env={
                "GIT_AUTHOR_DATE": f"@{int(stamp)} +0000",
                "GIT_COMMITTER_DATE": f"@{int(stamp)} +0000",
            },
        )
        if result.returncode:
            raise GitError("stack commit construction failed")
        current = result.stdout.strip()
    key = hashlib.sha256(repr((base, heads)).encode()).hexdigest()
    result = await git.arun_git_result(
        ["update-ref", "refs/aq/stacks/" + key, current], cwd=str(store)
    )
    if result.returncode:
        raise GitError("stack object retention failed")
    return current


class StackedBranches:
    def __init__(self, db, *, observer=None, clock=time.time, routing_policy=None):
        from src.integration.delivery_observer import prerequisite_observer

        self.db, self.clock = db, clock
        self.observer = observer or prerequisite_observer(db)
        self.routing_policy = routing_policy

    async def prepare(self, task_id, *, parent_base=None):
        """Return a freshly proven stack overlay; never reset the filing origin."""
        task = await self.db.get_task(task_id)
        project = await self.db.get_project(task.project_id) if task else None
        if task is None or not stacked_policy(project) or self.observer is None:
            return None
        view = await observe_stacks(self.observer, task.project_id, task_id=task_id)
        ids = view.required(task_id)
        if not ids:
            return None
        if not await view.fresh():
            raise GitError("prerequisite stack changed during preparation")
        async with self.db._engine.connect() as conn:
            proofs = await view.verified_on(conn, ids)
            origin = (
                (
                    await conn.execute(
                        select(task_branch_origins).where(
                            task_branch_origins.c.task_id == task_id,
                            task_branch_origins.c.retired_at.is_(None),
                        )
                    )
                )
                .mappings()
                .one()
            )
            parent = await conn.scalar(
                select(tasks.c.branch_name).where(
                    tasks.c.id == task.parent_task_id,
                )
            )
        if set(proofs) != ids:
            # Delivered sources may have had their task refs cleaned up. The
            # established parent proof remains a valid preparation route.
            if await self.db.hierarchy_prerequisite_delivery_head(task_id):
                return None
            raise GitError("prerequisite stack is incomplete or reopened")
        heads = [proofs[tid]["checkpoint_sha"] for tid in sorted(ids)]
        if len(heads) == 1 and parent_base is None:
            base = heads[0]
        else:
            parent_head = parent_base or view.snapshot.source_heads.get(
                "refs/remotes/origin/" + parent.removeprefix("refs/heads/"),
            )
            if not parent_head:
                raise GitError("stack parent branch is unavailable")
            base = await merge_heads(
                self.observer.git,
                view.snapshot.store,
                parent_head,
                heads,
                stamp=origin["created_at"],
            )
        snapshot = {"base_sha": base, "prerequisites": proofs}
        async with self.db.immediate() as conn:
            await conn.execute(
                select(tasks.c.id)
                .where(
                    tasks.c.id.in_(sorted(ids | {task_id})),
                )
                .order_by(tasks.c.id)
                .with_for_update()
            )
            if set(await view.verified_on(conn, ids)) != ids:
                raise GitError("prerequisite stack changed before recording its base")
            current = await self._origin(task_id, conn)
            previous = current["stack_snapshot"] or {}
            # Provenance belongs to the recorded dependent source. A fresh
            # workspace overlay must not relabel that source's old stack or
            # discard its recovery state. Refresh advances it after close.
            snapshot = previous or snapshot
            await conn.execute(
                update(task_branch_origins)
                .where(
                    task_branch_origins.c.id == origin["id"],
                    task_branch_origins.c.retired_at.is_(None),
                )
                .values(stack_snapshot=snapshot)
            )
        return {
            "base_sha": base,
            "prerequisite_head": base,
            "stack_store": view.snapshot.store,
            "stack_snapshot": snapshot,
        }

    async def _origin(self, task_id, conn):
        return (
            (
                await conn.execute(
                    select(task_branch_origins).where(
                        task_branch_origins.c.task_id == task_id,
                        task_branch_origins.c.retired_at.is_(None),
                    )
                )
            )
            .mappings()
            .one_or_none()
        )

    async def current(self, task_id, *, source_sha=None, conn=None):
        """DB identity guard used again at batch publication, without Git under locks."""
        if conn is None:
            async with self.db._engine.connect() as owned:
                return await self.current(task_id, source_sha=source_sha, conn=owned)
        origin = await self._origin(task_id, conn)
        stack = origin and origin["stack_snapshot"]
        if not stack:
            return True
        rows = await _inputs(
            conn,
            (
                await conn.scalar(
                    select(tasks.c.project_id).where(
                        tasks.c.id == task_id,
                    )
                )
            ),
            task_id,
        )
        proofs = {row["task_id"]: _identity(row) for row in rows}
        checkpoint = await conn.scalar(
            select(task_integration_checkpoints.c.checkpoint_sha).where(
                task_integration_checkpoints.c.task_id == task_id,
            )
        )
        return (
            proofs == stack["prerequisites"]
            and not stack.get("hold")
            and all(
                proof["status"] == "COMPLETED" and not proof["abandoned"]
                for proof in proofs.values()
            )
            and (
                source_sha is None
                or checkpoint == source_sha
                and stack.get("refreshed_head", source_sha) == source_sha
            )
        )

    async def source_contains_stack(self, task_id, source, snapshot):
        """Prove the actual completion contains every recorded prerequisite tip."""
        async with self.db._engine.connect() as conn:
            origin = await self._origin(task_id, conn)
        stack = origin and origin["stack_snapshot"]
        if not stack:
            return True
        observed = snapshot.observation
        for proof in stack["prerequisites"].values():
            remote = "refs/remotes/origin/" + proof["branch_name"].removeprefix("refs/heads/")
            remote_head = observed.source_heads.get(remote)
            if remote_head != proof["checkpoint_sha"]:
                if remote_head is not None:
                    return False
                async with self.db._engine.connect() as conn:
                    parent = await conn.scalar(
                        select(tasks.c.branch_name).where(tasks.c.id == proof["parent_task_id"])
                    )
                parent_head = observed.source_heads.get(
                    "refs/remotes/origin/" + (parent or "").removeprefix("refs/heads/")
                )
                if not parent_head or not await observed.git.ais_ancestor(
                    observed.store, proof["checkpoint_sha"], parent_head, strict=True
                ):
                    return False
            if not await observed.git.ais_ancestor(
                observed.store,
                proof["checkpoint_sha"],
                source,
                strict=True,
            ):
                return False
        return True

    async def _idle(self, conn, task_id):
        row = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().one()
        live = await conn.scalar(
            select(sessions.c.id)
            .where(
                sessions.c.task_id == task_id,
                sessions.c.state.in_(("starting", "running", "draining")),
            )
            .limit(1)
        )
        return row["status"] == "COMPLETED" and not row["assigned_agent_id"] and live is None

    async def _refresh_inputs(self, conn, task_id, origin):
        from src.integration.delivery_truth import load_delivery_requests

        checkpoint = (await conn.execute(select(task_integration_checkpoints).where(
            task_integration_checkpoints.c.task_id == task_id,
        ))).mappings().one_or_none()
        requests = await load_delivery_requests(
            self.db, (task_id,), repository_id=origin["repository_id"],
            target_ref="refs/heads/" + origin["parent_ref"].removeprefix("refs/heads/"),
            conn=conn, reduced=True,
        )
        request = requests.get(task_id)
        outcome = await conn.scalar(select(task_completion_records.c.outcome).where(
            task_completion_records.c.id == request.completion_id,
        )) if request else None
        return dict(checkpoint) if checkpoint else None, request, outcome

    async def _hold(self, origin, old, reason):
        async with self.db.immediate() as conn:
            result = await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.id == origin["id"],
                task_branch_origins.c.stack_snapshot == old,
                task_branch_origins.c.retired_at.is_(None),
            ).values(stack_snapshot={**old, "hold": reason}))
        return reason if result.rowcount else "changed"

    async def refresh(self, task_id, gitops, *, snapshot=None):
        """Merge changed sources into an idle child's branch or dispatch an ordinary worker.

        Transport holds the existing ref lease. Each refresh becomes an immutable
        completion source, so frozen batches cannot silently adopt a new tree.
        """
        from src.integration.lock import BranchLock
        from src.integration.models import BranchKey
        from src.integration.ownership import BranchBusy
        from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
        from src.integration.regeneration import GeneratedMergeConflict
        from src.integration.git_truth import GitTruth, GitTruthSnapshot

        if self.observer is None:
            return "unchanged"
        async with self.db._engine.connect() as conn:
            origin = await self._origin(task_id, conn)
            task = (await conn.execute(select(tasks).where(tasks.c.id == task_id))).mappings().one()
        old = origin and origin["stack_snapshot"]
        if not old:
            return "unchanged"
        async with self.db._engine.connect() as conn:
            if not await self._idle(conn, task_id):
                return "writer_active"
            source_inputs = await self._refresh_inputs(conn, task_id, origin)
        checkpoint, request, outcome = source_inputs
        view = await observe_stacks(self.observer, task["project_id"], task_id=task_id,
                                    snapshot=snapshot.observation if snapshot else None)
        if view.snapshot and not view.snapshot.error and request:
            truth = snapshot or GitTruthSnapshot(
                self.observer.truth or GitTruth(self.observer.git), view.snapshot,
            )
            repository = await self.db.get_repo(origin["repository_id"])
            root_ref = "refs/heads/" + repository.default_branch.removeprefix("refs/heads/")
            for ref in {request.target_ref, root_ref}:
                branch = ref.removeprefix("refs/heads/")
                view.watched_refs[branch] = view.snapshot.source_heads.get(
                    "refs/remotes/origin/" + branch,
                )
                evidence = await truth.for_target(ref).is_delivered(
                    replace(request, target_ref=ref), source_base=origin["base_sha"],
                )
                if evidence.satisfied:
                    return "already_delivered"
        ids = view.required(task_id)
        async with self.db._engine.connect() as conn:
            proofs = await view.verified_on(conn, ids)
        if set(proofs) != ids or not set(old["prerequisites"]) <= ids or not await view.fresh():
            await self._hold(origin, old, "prerequisite_reopened_or_unavailable")
            return "waiting_prerequisite"
        if (not checkpoint or not request or not request.completion_id
                or outcome != "pass"
                or request.repository_id != origin["repository_id"]
                or request.branch_name != task["branch_name"]
                or checkpoint["repository_id"] != origin["repository_id"]
                or checkpoint["branch"] != task["branch_name"]
                or not is_valid_git_oid(checkpoint["checkpoint_sha"] or "")
                or request.reported_source != checkpoint["checkpoint_sha"]):
            return await self._hold(origin, old, "source_unrecorded")
        repo = await gitops.repository(SimpleNamespace(repository_id=origin["repository_id"]))
        head = await gitops.remote(
            repo, "refs/heads/" + task["branch_name"].removeprefix("refs/heads/")
        )
        if not head:
            return await self._hold(origin, old, "source_missing")
        if head != checkpoint["checkpoint_sha"]:
            return await self._hold(origin, old, "source_changed")
        # Copy only immutable objects from the observer, including generated merge bases.
        await gitops.run(
            repo,
            "fetch",
            "--no-tags",
            view.snapshot.store,
            *sorted({head, *(proofs[tid]["checkpoint_sha"] for tid in ids),
                     *(proof["checkpoint_sha"] for proof in old["prerequisites"].values())}),
        )
        heads = [proofs[tid]["checkpoint_sha"] for tid in sorted(ids)]
        for tid, previous in old["prerequisites"].items():
            if not await gitops.is_ancestor(repo, previous["checkpoint_sha"],
                                           proofs[tid]["checkpoint_sha"]):
                return await self._hold(origin, old, "prerequisite_superseded")
        if (
            proofs == old["prerequisites"]
            and not old.get("repair_task_id")
            and all([await gitops.is_ancestor(repo, source, head) for source in heads])
        ):
            if old.get("hold") or old.get("refreshed_head", head) != head:
                # Clear transient holds and recognize a newly recorded child
                # completion without creating another completion or generation.
                async with self.db.immediate() as conn:
                    await conn.execute(select(tasks.c.id).where(
                        tasks.c.id.in_(sorted(ids | {task_id})),
                    ).order_by(tasks.c.id).with_for_update())
                    if (not await self._idle(conn, task_id)
                            or await self._refresh_inputs(conn, task_id, origin) != source_inputs
                            or await view.verified_on(conn, ids) != proofs):
                        return "changed"
                    cleared = {key: value for key, value in old.items() if key != "hold"}
                    if "refreshed_head" in cleared:
                        cleared["refreshed_head"] = head
                    result = await conn.execute(update(task_branch_origins).where(
                        task_branch_origins.c.id == origin["id"],
                        task_branch_origins.c.stack_snapshot == old,
                        task_branch_origins.c.retired_at.is_(None),
                    ).values(stack_snapshot=cleared))
                    if not result.rowcount:
                        return "changed"
            return "unchanged"
        starting = checkpoint["checkpoint_sha"]
        repair_id = old.get("repair_task_id")
        if repair_id:
            repair = await self.db.get_task(repair_id)
            if repair is None or repair.status.value != "COMPLETED":
                return "repair_pending"
            completion = await self.db.get_task_completion(repair_id)
            if completion is None or completion.outcome != "pass" or not completion.commits:
                return "repair_pending"
            repair_head = completion.commits[-1]
            if await gitops.remote(repo, "refs/heads/" + repair.branch_name) != repair_head:
                return "repair_pending"
            await gitops.run(repo, "fetch", "--no-tags", "origin", repair_head)
            if not await gitops.is_ancestor(repo, head, repair_head):
                return "repair_pending"
            starting = repair_head
        try:
            merged = await merge_heads(
                gitops.git, repo.store, starting, heads, stamp=origin["created_at"]
            )
        except GeneratedMergeConflict:
            return await self._file_repair(task, origin, old, proofs, starting, repo, gitops)
        new_stack = {"base_sha": old["base_sha"], "prerequisites": proofs, "refreshed_head": merged}
        locks = BranchLock(self.db)
        target = BranchKey(repository_id=origin["repository_id"], branch=task["branch_name"])
        try:
            fence = await locks.acquire(target, "service:stack-refresh", role="integration")
        except BranchBusy:
            return "writer_active"

        async def authorize():
            async with self.db._engine.connect() as conn:
                current = await self._origin(task_id, conn)
                return (
                    await self._idle(conn, task_id)
                    and current is not None
                    and current["id"] == origin["id"]
                    and current["stack_snapshot"] == old
                    and await view.verified_on(conn, ids) == proofs
                    and await self._refresh_inputs(conn, task_id, origin) == source_inputs
                )

        try:
            if not await authorize() or not await view.fresh():
                return "changed"
            await locks.fenced_push(
                fence,
                git=gitops.git,
                checkout_path=str(repo.store),
                repository=repo.binding,
                tip_oid=merged,
                expected_old_oid=head,
                authorize=authorize,
            )
            completion_id = (
                "stack-" + hashlib.sha256(repr((task_id, merged, proofs)).encode()).hexdigest()
            )
            await GitProvenance(
                gitops.git,
                str(repo.store),
                repository_url=(await self.db.get_repo(repo.repository_id)).url,
            ).write_completion(
                CompletedSource(
                    CompletionIdentity(
                        task["project_id"],
                        origin["repository_id"],
                        task_id,
                        completion_id,
                    ),
                    merged,
                ),
                claim_epoch=task["claim_epoch"],
            )
            async with self.db.immediate() as conn:
                await conn.execute(
                    select(tasks.c.id)
                    .where(
                        tasks.c.id.in_(sorted(ids | {task_id})),
                    )
                    .order_by(tasks.c.id)
                    .with_for_update()
                )
                current = await self._origin(task_id, conn)
                if (
                    current is None
                    or current["id"] != origin["id"]
                    or current["stack_snapshot"] != old
                    or await view.verified_on(conn, ids) != proofs
                    or not await self._idle(conn, task_id)
                    or await self._refresh_inputs(conn, task_id, origin) != source_inputs
                ):
                    return "changed"
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                from src.database.queries.task_queries import DEVELOPMENT_COMPLETION_ID_KEY

                await conn.execute(
                    pg_insert(task_completion_records)
                    .values(
                        id=completion_id,
                        task_id=task_id,
                        outcome="pass",
                        branch=task["branch_name"],
                        commits=json.dumps([merged]),
                        completed_at=self.clock(),
                        summary="Merged current prerequisite heads into the preserved dependent source.",
                    )
                    .on_conflict_do_nothing(index_elements=["id"])
                )
                marker = pg_insert(task_metadata).values(
                    task_id=task_id,
                    key=DEVELOPMENT_COMPLETION_ID_KEY,
                    value=json.dumps(completion_id),
                )
                await conn.execute(
                    marker.on_conflict_do_update(
                        index_elements=["task_id", "key"], set_={"value": marker.excluded.value}
                    )
                )
                await conn.execute(
                    update(task_branch_origins)
                    .where(
                        task_branch_origins.c.id == origin["id"],
                    )
                    .values(stack_snapshot=new_stack)
                )
                await conn.execute(
                    update(task_integration_checkpoints)
                    .where(
                        task_integration_checkpoints.c.task_id == task_id,
                    )
                    .values(
                        checkpoint_sha=merged,
                        generation=task_integration_checkpoints.c.generation + 1,
                        version=task_integration_checkpoints.c.version + 1,
                        updated_at=self.clock(),
                    )
                )
            return "refreshed"
        finally:
            await locks.release(fence)

    async def _file_repair(self, task, origin, old, proofs, head, repo, gitops):
        """File once on an isolated repair branch, preserving the dependent's source."""
        from src.integration.epic_branch import reserve_branch_name
        from src.integration.hierarchy import HierarchyIntegration
        from src.integration.lock import BranchLock
        from src.integration.models import BranchKey
        from src.models import Task, TaskStatus, TaskType
        from src.task_names import child_task_id

        if old.get("repair_task_id"):
            previous = await self.db.get_task(old["repair_task_id"])
            if previous is None or previous.status is not TaskStatus.COMPLETED:
                return "repair_pending"
        async with self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, task["project_id"])
            current = await self._origin(task["id"], conn)
            if current["stack_snapshot"] != old:
                return "changed"
            repair_id, _capped = await child_task_id(conn, task["parent_task_id"])
            repair_task = Task(
                id=repair_id,
                project_id=task["project_id"],
                repo_id=origin["repository_id"],
                title=f"Repair prerequisite stack for {task['id']}",
                description=(
                    f"Preserve dependent {task['id']} at {head}. Your isolated branch starts "
                    "there. Merge these current prerequisite heads, resolve conflicts, push and close: "
                    + json.dumps(proofs, sort_keys=True)
                ),
                task_type=TaskType.BUGFIX,
                status=TaskStatus.DEFINED,
                priority=task["priority"],
                class_hint="deep-high",
                created_by_kind="system",
                created_by_id=task["id"],
            )
            await self.db.create_task(repair_task, conn=conn)
            branch = await reserve_branch_name(
                conn, task_id=repair_id, title="Repair prerequisite stack", is_epic=False
            )
            await self.db.set_parent(
                repair_id,
                task["parent_task_id"],
                conn=conn,
                integration_authorized=True,
                completed_parent_for_repair=True,
            )
            hierarchy = HierarchyIntegration(self.db)
            parent_checkpoint = await hierarchy._locked_checkpoint(conn, task["parent_task_id"])
            generation = parent_checkpoint["generation"] + 1
            await hierarchy._bump_checkpoint(
                conn, task["parent_task_id"], parent_checkpoint["generation"], generation
            )
            repair_task.parent_task_id = task["parent_task_id"]
            await hierarchy._maybe_create_routing_gate(conn, repair_task, self.routing_policy)
            reserved = await hierarchy._reserve_origin(
                conn,
                task_id=repair_id,
                repository_id=origin["repository_id"],
                parent_task_id=task["parent_task_id"],
                parent_ref=origin["parent_ref"],
                base_sha=head,
                generation=generation,
            )
            await hierarchy._insert_checkpoint(
                conn,
                task_id=repair_id,
                repository_id=origin["repository_id"],
                branch=branch,
                checkpoint_sha=head,
            )
            await conn.execute(
                update(task_branch_origins)
                .where(
                    task_branch_origins.c.id == origin["id"],
                )
                .values(
                    stack_snapshot={**old, "hold": "stack_conflict", "repair_task_id": repair_id}
                )
            )
            await conn.execute(
                insert(task_metadata).values(
                    task_id=repair_id, key="stack_repair_for", value=json.dumps(task["id"])
                )
            )
        # Materialization is retried by the normal origin scanner after any failed push.
        locks = BranchLock(self.db)
        fence = await locks.acquire(
            BranchKey(repository_id=repo.repository_id, branch=branch), repair_id
        )
        try:

            async def authorize():
                return (await self.db.get_task(repair_id)).status is TaskStatus.DEFINED

            await locks.fenced_push(
                fence,
                git=gitops.git,
                checkout_path=str(repo.store),
                repository=repo.binding,
                tip_oid=head,
                expected_old_oid="0" * 40,
                authorize=authorize,
            )
            async with self.db.immediate() as conn:
                await conn.execute(
                    update(task_branch_origins)
                    .where(
                        task_branch_origins.c.id == reserved["id"],
                    )
                    .values(materialized=True, materialized_at=self.clock())
                )
                transition = await self.db._apply_transition(
                    conn, repair_id, TaskStatus.READY, context="origin_materialized"
                )
            await self.db._notify_ready(transition.ready)
        finally:
            await locks.release(fence)
        return "repair_filed"


class EpicRefreshPending(ValueError):
    """Normal admission wait while the train checks or repairs an epic."""


class EpicRefresh:
    def __init__(self, db, train=None, *, clock=time.time):
        self.db, self.train, self.clock = db, train, clock

    async def identity(self, task_id, *, conn=None):
        child = tasks.alias("refresh_epic_child")
        query = select(tasks.c.id, tasks.c.project_id, tasks.c.branch_name,
                       tasks.c.repo_id, repos.c.default_branch).select_from(
            tasks.join(projects, projects.c.id == tasks.c.project_id)
            .join(repos, repos.c.id == projects.c.integration_repository_id)
        ).where(tasks.c.id == task_id, tasks.c.repo_id == repos.c.id,
                projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                projects.c.status == "ACTIVE",
                tasks.c.branch_name.is_not(None),
                select(child.c.id).where(child.c.parent_task_id == task_id).exists())
        if conn is None:
            async with self.db._engine.connect() as owned:
                return (await owned.execute(query)).mappings().one_or_none()
        return (await conn.execute(query)).mappings().one_or_none()

    async def inspect(self, task_id, *, snapshot=None):
        from src.integration.train_sources import project_snapshot

        row = await self.identity(task_id)
        if row is None:
            raise ValueError("task is not an epic in its project's integration repository")
        ref = "refs/heads/" + row["branch_name"].removeprefix("refs/heads/")
        default = "refs/heads/" + row["default_branch"].removeprefix("refs/heads/")
        if ref == default:
            raise ValueError("an epic cannot own the default branch")
        target = TrainTarget(row["project_id"], row["repo_id"], ref, "epic")
        observed = snapshot.for_target(ref) if snapshot else await project_snapshot(self.db, target)
        if observed is None or observed.error or not observed.target_oid:
            raise ValueError("epic branch cannot be observed")
        main = observed.for_target(default)
        if main.error or not main.target_oid:
            raise ValueError("default branch cannot be observed")
        git, store = observed.observation.git, observed.observation.store
        counts = await git.arun_git_result(
            ["rev-list", "--left-right", "--count", f"{observed.target_oid}...{main.target_oid}"],
            cwd=store,
        )
        if counts.returncode:
            raise ValueError("epic distance cannot be observed")
        ahead, behind = map(int, counts.stdout.split())
        result = {"task_id": task_id, "project_id": row["project_id"], "target_ref": ref,
                  "target_sha": observed.target_oid, "default_ref": default,
                  "default_sha": main.target_oid, "ahead": ahead, "behind": behind}
        return row, target, observed, result

    async def refresh(self, task_id, *, dry_run=True, operator_id=None):
        from src.integration.train_sources import DatabaseBatches

        row, target, observed, result = await self.inspect(task_id)
        if dry_run:
            return {**result, "outcome": "preview", "dry_run": True}
        batches = DatabaseBatches(self.db, clock=self.clock)
        current = await batches.current(target)
        if current is not None and not current.epic_refresh:
            return {**result, "outcome": "pending", "dry_run": False, "batch_id": current.id}
        if current is None and not result["behind"]:
            return {**result, "outcome": "current", "dry_run": False}
        if self.train is None:
            raise ValueError("the Git-first integration train is unavailable")
        if current is None:
            batch, member = await self._batch(task_id, target, observed, result)
            current = await self._freeze(row, observed, batch, member)
        visit = await self.train.request_visit(target)
        if visit is None:
            return {**result, "outcome": "pending", "dry_run": False,
                    "batch_id": current.id, "state": "queued"}
        if operator_id:
            await self.db.log_event("integration.epic_refresh", project_id=row["project_id"],
                task_id=task_id, payload=json.dumps({**result, "batch_id": current.id,
                    "operator_id": operator_id, "state": visit.state}))
        return {**result, "target_sha": visit.target_sha or result["target_sha"],
                "outcome": "refreshed" if visit.state == "delivered" else "pending",
                "dry_run": False, "batch_id": current.id, "state": visit.state,
                "candidate_sha": visit.candidate_sha, "detail": visit.detail}

    async def start(self, task_id, *, snapshot=None):
        """Freeze the refresh of the epic's current (head, default head) pair.

        The train's own visits run it; nothing here waits for one. A pair whose
        refresh batch already exists, settled or not, is never started again.
        """
        from src.integration.train_sources import DatabaseBatches

        row, target, observed, result = await self.inspect(task_id, snapshot=snapshot)
        current = await DatabaseBatches(self.db, clock=self.clock).current(target)
        if current is not None:
            return {**result, "outcome": "running" if current.epic_refresh else "pending",
                    "batch_id": current.id}
        if not result["behind"]:
            return {**result, "outcome": "current"}
        batch, member = await self._batch(task_id, target, observed, result)
        if await BatchStore(self.db, clock=self.clock).get(batch.id) is not None:
            return {**result, "outcome": "settled", "batch_id": batch.id}
        await self._freeze(row, observed, batch, member)
        return {**result, "outcome": "started", "batch_id": batch.id}

    async def _batch(self, task_id, target, observed, result):
        """The deterministic refresh batch of one (epic head, default head) pair."""
        git, store = observed.observation.git, observed.observation.store
        base = await git.arun_git_result(
            ["merge-base", result["target_sha"], result["default_sha"]], cwd=store,
        )
        if base.returncode:
            raise ValueError("epic and default branch have no common base")
        member = BatchMember(task_id, result["default_sha"], base.stdout.strip())
        key = hashlib.sha256(repr((target.key, member, result["target_sha"])).encode()).hexdigest()
        return Batch("train-epic-refresh-" + key, target.project_id, target.repository_id,
                     target.target_ref, created_at=self.clock()), member

    async def _freeze(self, row, observed, batch, member):
        git, store = observed.observation.git, observed.observation.store
        tree = await git.atree_sha(store, member.source_sha)
        if await self.identity(member.task_id) != row or not await observed.is_fresh():
            raise ValueError("epic identity changed during refresh")
        return await BatchStore(self.db, clock=self.clock).freeze(
            batch, (member,), trees={member.task_id: tree})

    async def child_base(self, task, origin):
        if not task.parent_task_id:
            return None
        project = await self.db.get_project(task.project_id)
        policy = (project.hierarchical_integration_policy or {})
        if policy.get("cross_epic_prerequisites") == "completed":
            return None
        source = tasks.alias("refresh_child_source")
        async with self.db._engine.connect() as conn:
            cross = await conn.scalar(select(source.c.id).select_from(task_dependencies.join(
                source, source.c.id == task_dependencies.c.depends_on_task_id,
            )).where(task_dependencies.c.task_id == task.id, task_dependencies.c.dep_type == "blocks",
                     source.c.parent_task_id.is_distinct_from(task.parent_task_id)
                     | source.c.parent_task_id.is_(None)).limit(1))
        if cross is None:
            return None
        observer = prerequisite_observer(self.db)
        if observer is None:
            raise ValueError("cross-epic prerequisite Git truth is unavailable")
        view = await observer.prerequisite_view(task.project_id, task_id=task.id)
        if not await view.fresh():
            raise ValueError("cross-epic prerequisite target changed")
        row, target, observed, result = await self.inspect(task.parent_task_id)
        from src.integration.train_sources import DatabaseBatches

        async def contains_prerequisites():
            from src.integration.delivery_truth import DeliveryState

            if not view.default.evidence:
                return False
            for proof in view.default.evidence.values():
                if proof.state is DeliveryState.NO_ARTIFACT:
                    continue
                if not proof.satisfied or not proof.source_oid:
                    return False
                contained = await observed.is_delivered(
                    replace(proof.request, target_ref=target.target_ref),
                    source_base=proof.source_base,
                )
                if not contained.satisfied or contained.source_oid != proof.source_oid:
                    return False
            return True

        refreshed = {}
        if not await contains_prerequisites():
            # Only a child that needs the refresh waits on the epic's open batch;
            # one whose epic already holds every cross-epic source starts now.
            current = await DatabaseBatches(self.db).current(target)
            if current is not None:
                raise EpicRefreshPending(f"epic refresh pending: {current.id}")
            refreshed = await self.refresh(task.parent_task_id, dry_run=False)
            if refreshed["outcome"] == "pending":
                raise EpicRefreshPending(f"epic refresh pending: {refreshed['batch_id']}")
            row, target, observed, result = await self.inspect(task.parent_task_id)
            # Main can advance during CI without invalidating the contained
            # prerequisite sources. Observe their current identities again.
            view = await observer.prerequisite_view(task.project_id, task_id=task.id)
        if (not await contains_prerequisites() or not await observed.is_fresh()
                or not await view.fresh()):
            raise ValueError("epic refresh or default branch changed during child preparation")
        if (origin["parent_task_id"] != task.parent_task_id
                or origin["parent_repository_id"] != target.repository_id
                or origin["parent_ref"].removeprefix("refs/heads/") != target.target_ref.removeprefix(
                    "refs/heads/")):
            raise ValueError("child origin no longer names the refreshed epic")
        annotation = {"parent_ref": target.target_ref, "head_sha": result["target_sha"],
                      "default_ref": result["default_ref"], "default_sha": result["default_sha"],
                      "batch_id": refreshed.get("batch_id")}
        async with self.db.immediate() as conn:
            await conn.execute(select(tasks.c.id).where(
                tasks.c.id.in_(sorted({task.id, task.parent_task_id, *view.all_ids})),
            ).order_by(tasks.c.id).with_for_update())
            live_parent = await conn.scalar(select(tasks.c.parent_task_id).where(tasks.c.id == task.id))
            required = set((await conn.execute(select(source.c.id).select_from(
                task_dependencies.join(source, source.c.id == task_dependencies.c.depends_on_task_id),
            ).where(task_dependencies.c.task_id == task.id, task_dependencies.c.dep_type == "blocks",
                    source.c.parent_task_id.is_distinct_from(live_parent)
                    | source.c.parent_task_id.is_(None)))).scalars())
            current = await view.default.verified_on(conn, view.default.evidence)
            if (live_parent != task.parent_task_id or not required or current.keys() != required
                    or any(not proof.satisfied for proof in current.values())):
                raise ValueError("cross-epic prerequisite identity changed during preparation")
            saved = await conn.execute(update(task_branch_origins).where(
                task_branch_origins.c.id == origin["id"],
                task_branch_origins.c.retired_at.is_(None),
                task_branch_origins.c.base_sha == origin["base_sha"],
                task_branch_origins.c.parent_task_id == task.parent_task_id,
                task_branch_origins.c.parent_repository_id == target.repository_id,
                task_branch_origins.c.parent_ref == origin["parent_ref"],
            ).values(base_refresh=annotation))
            if saved.rowcount != 1:
                raise ValueError("child origin changed during refresh")
        return result["target_sha"]
