"""Development delivery: Git manifests, short publication exclusion, and recoverable facts.

No synthetic review/CI receipts are written. Legacy episodes remain audit history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
import time
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, text, update

from src.database.queries.blocked_state import blocked_predicate
from src.database.tables import development_deliveries as deliveries
from src.database.tables import projects, sessions, tasks
from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.models import TaskStatus


class DevelopmentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation: str = "focused"
    commands: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(default=300, gt=0, le=3600)
    interval_seconds: int = Field(default=300, gt=0)
    max_batch_size: int = Field(default=50, gt=0, le=500)

    def checked(self):
        if self.validation not in {"focused", "advisory", "none"}:
            raise ValueError("validation must be focused, advisory, or none")
        if self.validation == "focused" and not self.commands:
            raise ValueError("focused validation requires at least one local command")
        return self


class DevelopmentBusy(RuntimeError):
    pass


class DevelopmentIntegration:
    def __init__(self, db, *, data_dir, git=None, confirm_stopped=None):
        self.db = db
        self.data_dir = Path(data_dir) / "development-integration"
        self.git = git or GitManager()
        self.next_due = {}
        self.confirm_stopped = confirm_stopped

    async def on_task_completed(self, event):
        """Wake delivery without doing Git or validation in the completion path.

        The next integration cycle coalesces completions into one batch. Removing
        the deadline also preserves a wake arriving during an in-flight sweep:
        tick sets the next deadline before awaiting the publisher, not after it.
        Periodic sweeps remain the recovery path for missed events and restarts.
        """
        self.next_due.pop(event.get("project_id"), None)

    @asynccontextmanager
    async def exclusion(self, repository_id):
        # Dedicated connection owns a session advisory lock across short DB commits.
        # A process death releases it; durable publishing rows retain ambiguous writes.
        key = int.from_bytes(
            hashlib.sha256(repository_id.encode()).digest()[:8], "big", signed=True
        )
        async with self.db._engine.connect() as conn:
            acquired = await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
            if not acquired:
                raise DevelopmentBusy("repository publisher is already running")
            try:
                yield
            finally:
                await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})

    async def run_git(self, store, *args):
        result = await self.git.arun_git_result(list(args), cwd=str(store))
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "Git command failed")
        return result.stdout.strip()

    async def store(self, repo):
        path = self.data_dir / hashlib.sha256(repo.id.encode()).hexdigest()[:20] / "repository"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            await self.run_git(path.parent, "clone", "--no-checkout", "--", repo.url, str(path))
        if await self.run_git(path, "remote", "get-url", "origin") != repo.url:
            raise ValueError("retained repository URL differs from configured repository")
        await self.run_git(path, "fetch", "--prune", "origin")
        return path

    async def remote(self, store, ref):
        await self.run_git(store, "check-ref-format", ref)
        value = await self.run_git(store, "ls-remote", "--refs", "origin", ref)
        return value.split()[0] if value else None

    async def save(self, row):
        async with self.db._engine.begin() as conn:
            await conn.execute(insert(deliveries).values(**row))
            flipped = await self.db.recompute_blocked(
                {member["task_id"] for member in row["manifest"]}, conn=conn
            )
            ready = await self.db._note_frontier_entry(conn, flipped, reason="unblocked")
        await self.db.log_blocked_flips(flipped)
        await self.db._notify_ready([(task_id, "unblocked") for task_id in ready])

    async def change(self, identity, **values):
        async with self.db._engine.begin() as conn:
            result = await conn.execute(
                update(deliveries)
                .where(deliveries.c.id == identity)
                .values(**values, updated_at=time.time())
                .returning(deliveries.c.manifest)
            )
            manifest = result.scalar_one_or_none() or []
            flipped = await self.db.recompute_blocked(
                {member["task_id"] for member in manifest}, conn=conn
            )
            ready = await self.db._note_frontier_entry(conn, flipped, reason="unblocked")
        await self.db.log_blocked_flips(flipped)
        await self.db._notify_ready([(task_id, "unblocked") for task_id in ready])

    async def rows(self, project_id):
        async with self.db._engine.connect() as conn:
            return [
                dict(r)
                for r in (
                    await conn.execute(
                        select(deliveries)
                        .where(deliveries.c.project_id == project_id)
                        .order_by(deliveries.c.created_at)
                    )
                ).mappings()
            ]

    async def refresh_dependencies(self, project_id):
        """Repair projections from before delivery-aware readiness was installed."""
        async with self.db._engine.begin() as conn:
            ids = set((await conn.execute(
                select(tasks.c.id).where(tasks.c.project_id == project_id)
            )).scalars())
            flipped = await self.db.recompute_blocked(ids, conn=conn)
            ready = await self.db._note_frontier_entry(conn, flipped, reason="unblocked")
        await self.db.log_blocked_flips(flipped)
        await self.db._notify_ready([(task_id, "unblocked") for task_id in ready])

    async def reconcile(self, repo, store):
        for row in await self.rows(repo.project_id):
            if row["repository_id"] != repo.id or row["state"] != "publishing":
                continue
            actual = await self.remote(store, row["target_ref"])
            prepared = row["prepared_sha"]
            if actual == prepared or (
                actual and prepared and await self.git.ais_ancestor(str(store), prepared, actual)
            ):
                await self.change(
                    row["id"],
                    state="delivered",
                    evidence=row["evidence"] | {"reconciled_remote": actual},
                )
            elif actual == row["expected_sha"]:
                # Caller holds publication exclusion; no daemon publisher survived.
                await self.change(
                    row["id"],
                    state="parked",
                    evidence=row["evidence"] | {"reconciled": "not_applied"},
                )
            else:
                raise DevelopmentBusy(
                    f"remote write {row['id']} needs reconciliation: target changed"
                )

    async def validate(self, store, policy):
        evidence = {"kind": "local", "validation": policy.validation, "checks": []}
        if policy.validation == "none":
            evidence["conclusion"] = "not_run"
            return evidence, True
        for command in policy.commands:
            process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                command,
                cwd=str(store),
                start_new_session=True,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                output, _ = await asyncio.wait_for(process.communicate(), policy.timeout_seconds)
                code = process.returncode
            except (TimeoutError, asyncio.CancelledError):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                await process.wait()
                if asyncio.current_task().cancelling():
                    raise
                output, code = b"validation timed out", 124
            evidence["checks"].append(
                {
                    "command": command,
                    "exit_code": code,
                    "output": output.decode(errors="replace")[-8000:],
                }
            )
        passed = bool(evidence["checks"]) and all(c["exit_code"] == 0 for c in evidence["checks"])
        evidence["conclusion"] = (
            "passed" if passed else "failed" if evidence["checks"] else "not_run"
        )
        return evidence, passed or policy.validation == "advisory"

    async def publish(self, repo, store, target_ref, head, expected, manifest, evidence, reason):
        now = time.time()
        row = {
            "id": str(uuid4()),
            "project_id": repo.project_id,
            "repository_id": repo.id,
            "target_ref": target_ref,
            "expected_sha": expected,
            "prepared_sha": head,
            "state": "prepared",
            "manifest": manifest,
            "evidence": evidence,
            "reason": reason,
            "created_at": now,
            "updated_at": now,
        }
        await self.save(row)
        actual = await self.remote(store, target_ref)
        if actual != expected:
            await self.change(
                row["id"], state="parked", evidence=evidence | {"reason": "base_moved"}
            )
            return {"outcome": "base_moved", "id": row["id"]}
        if expected and not await self.git.ais_ancestor(str(store), expected, head):
            raise ValueError("development publication must preserve target history")
        await self.change(row["id"], state="publishing")
        # Lease binds the actual remote old SHA; zero means ref must not exist.
        await self.run_git(
            store,
            "push",
            f"--force-with-lease={target_ref}:{expected or '0' * 40}",
            "origin",
            f"{head}:{target_ref}",
        )
        confirmed = await self.remote(store, target_ref)
        if confirmed != head:
            raise DevelopmentBusy("published ref could not be confirmed; journal retained")
        await self.change(row["id"], state="delivered")
        return {"outcome": "delivered", "id": row["id"], "head_sha": head, "manifest": manifest}

    async def adopt(
        self,
        *,
        project_id,
        task_ids,
        target_ref,
        head_sha,
        reason,
        operator_id,
        accept_equivalent=False,
    ):
        if not reason.strip() or not is_valid_git_oid(head_sha) or not task_ids:
            raise ValueError("task ids, exact target SHA and reason are required")
        project = await self.db.get_project(project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated repository")
        repo = await self.db.get_repo(project.integration_repository_id)
        async with self.exclusion(repo.id):
            store = await self.store(repo)
            if await self.remote(store, target_ref) != head_sha:
                raise ValueError("target ref is no longer at the supplied SHA")
            manifest = []
            for task_id in sorted(set(task_ids)):
                task = await self.db.get_task(task_id)
                if (
                    task is None
                    or task.project_id != project_id
                    or task.repo_id not in {None, repo.id}
                ):
                    raise ValueError(f"task {task_id} does not belong to the repository project")
                source = (
                    await self.remote(
                        store, "refs/heads/" + task.branch_name.removeprefix("refs/heads/")
                    )
                    if task.branch_name
                    else None
                )
                contained = bool(
                    source and await self.git.ais_ancestor(str(store), source, head_sha)
                )
                if not contained and not accept_equivalent:
                    raise ValueError(
                        f"{task_id}: source is not an ancestor; explicit equivalence acceptance required"
                    )
                manifest.append(
                    {
                        "task_id": task_id,
                        "source_sha": source,
                        "acceptance": "ancestry" if contained else "operator_equivalent",
                    }
                )
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, project_id)
                selected = (
                    (
                        await conn.execute(
                            select(tasks).where(tasks.c.id.in_(task_ids)).with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                held = await conn.scalar(
                    select(sessions.c.id)
                    .where(sessions.c.task_id.in_(task_ids), sessions.c.state != "stopped")
                    .limit(1)
                )
                if held or any(t["assigned_agent_id"] for t in selected):
                    raise DevelopmentBusy("selected tasks still have a worker assignment")
                outside = await conn.scalar(
                    select(tasks.c.id)
                    .where(
                        tasks.c.parent_task_id.in_(task_ids),
                        tasks.c.id.not_in(task_ids),
                        tasks.c.status.not_in(["COMPLETED", "CANCELLED"]),
                    )
                    .limit(1)
                )
                if outside:
                    raise ValueError(f"required child {outside} is still open")
                now = time.time()
                identity = str(uuid4())
                await conn.execute(
                    insert(deliveries).values(
                        id=identity,
                        project_id=project_id,
                        repository_id=repo.id,
                        target_ref=target_ref,
                        expected_sha=head_sha,
                        prepared_sha=head_sha,
                        state="adopted",
                        manifest=manifest,
                        evidence={
                            "kind": "operator_accepted",
                            "operator_id": operator_id,
                            "validation": "operator_decision",
                            "conclusion": "not_ci_attested",
                        },
                        reason=reason,
                        created_at=now,
                        updated_at=now,
                    )
                )
                from src.database.queries.task_queries import _OPERATOR_ADOPTION_TOKEN

                results = []
                pending_tasks = {t["id"]: t for t in selected}
                close_order = []
                while pending_tasks:
                    parent_ids = {t["parent_task_id"] for t in pending_tasks.values()}
                    leaves = sorted(set(pending_tasks) - parent_ids)
                    if not leaves:
                        raise ValueError("task hierarchy contains a cycle")
                    close_order.extend(leaves)
                    for identity_task in leaves:
                        pending_tasks.pop(identity_task)
                from src.database.tables import task_completion_records

                for task_id in close_order:
                    if next(t for t in selected if t["id"] == task_id)["status"] != "COMPLETED":
                        await conn.execute(
                            insert(task_completion_records).values(
                                id=str(uuid4()),
                                task_id=task_id,
                                outcome="pass",
                                summary=reason,
                                verification="Operator adoption; not CI attested",
                                commits=json.dumps([head_sha]),
                                completed_at=now,
                                notes=f"Delivery journal {identity}; operator {operator_id}",
                            )
                        )
                    results.append(
                        await self.db._apply_transition(
                            conn,
                            task_id,
                            TaskStatus.COMPLETED,
                            context="operator_adopt_delivery",
                            _operator_adoption_token=_OPERATOR_ADOPTION_TOKEN,
                            _manual_pause_control=True,
                        )
                    )
            for result in results:
                await self.db.log_blocked_flips(result.flipped)
                await self.db._notify_settled(result.settled)
                await self.db._notify_ready(result.ready)
            return {
                "outcome": "adopted",
                "id": identity,
                "head_sha": head_sha,
                "manifest": manifest,
            }

    async def sweep(self, project_id, *, retry=False):
        project = await self.db.get_project(project_id)
        if project is None or project.hierarchical_integration_mode != "development":
            raise ValueError("project is not in development mode")
        policy = DevelopmentPolicy.model_validate(project.hierarchical_integration_policy).checked()
        repo = await self.db.get_repo(project.integration_repository_id)
        await self.refresh_dependencies(project_id)
        async with self.exclusion(repo.id):
            store = await self.store(repo)
            await self.reconcile(repo, store)
            target = "refs/heads/" + repo.default_branch
            base = await self.remote(store, target)
            if not base:
                raise ValueError("default branch does not exist")
            await self.run_git(store, "checkout", "--detach", "--force", base)
            history = await self.rows(project_id)
            done = {
                (m["task_id"], m.get("source_sha"))
                for r in history
                if r["state"] in {"delivered", "adopted"} and r["target_ref"] == target
                for m in r["manifest"]
            }
            parked = (
                {
                    (m["task_id"], m.get("source_sha"))
                    for r in history
                    if r["state"] == "parked"
                    for m in r["manifest"]
                }
                if not retry
                else set()
            )
            manifest, conflicts = [], []
            async with self.db._engine.connect() as conn:
                candidates = (
                    (
                        await conn.execute(
                            select(tasks)
                            .where(
                                tasks.c.project_id == project_id,
                                tasks.c.status == "COMPLETED",
                                (tasks.c.repo_id == repo.id) | tasks.c.repo_id.is_(None),
                                tasks.c.branch_name.is_not(None),
                                # Completion chains can be assembled in this
                                # batch. Keep gates and unfinished dependencies,
                                # but do not wait for our own earlier publication.
                                ~blocked_predicate(include_development_delivery=False),
                            )
                            .order_by(tasks.c.updated_at, tasks.c.id)
                        )
                    )
                    .mappings()
                    .all()
                )
            from src.database.tables import task_dependencies

            async with self.db._engine.connect() as conn:
                links = (
                    (
                        await conn.execute(
                            select(task_dependencies).where(
                                task_dependencies.c.task_id.in_([t["id"] for t in candidates]),
                                task_dependencies.c.dep_type.in_(
                                    ["blocks", "waits-for", "conditional-blocks"]
                                ),
                            )
                        )
                    )
                    .mappings()
                    .all()
                )
            dependencies = {}
            for link in links:
                dependencies.setdefault(link["task_id"], set()).add(link["depends_on_task_id"])
            remaining = {t["id"]: t for t in candidates}
            ordered = []
            while remaining:
                eligible = [
                    t
                    for t in remaining.values()
                    if not (dependencies.get(t["id"], set()) & remaining.keys())
                ]
                if not eligible:
                    break  # Invalid cyclic graph is never guessed through.
                for task in eligible:
                    ordered.append(task)
                    remaining.pop(task["id"])
            unavailable = {task_id for task_id, _ in parked}
            head = base
            parent_heads = {}
            blocked_parents = set()
            assembly_id = uuid4().hex[:12]
            # Fetch above pins one remote snapshot. Do not make a network request
            # for every historical task branch on every sweep.
            fetched = await self.run_git(
                store, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/origin/"
            )
            source_heads = dict(line.split(" ", 1) for line in fetched.splitlines())
            for task in ordered:
                if task["parent_task_id"] in blocked_parents:
                    unavailable.add(task["id"])
                    continue
                if dependencies.get(task["id"], set()) & unavailable:
                    unavailable.add(task["id"])
                    continue
                source = source_heads.get(
                    "refs/remotes/origin/" + task["branch_name"].removeprefix("refs/heads/")
                )
                if not source:
                    # Branch cleanup after merge must not strand every later
                    # batch. A durable completion plus default-branch ancestry
                    # proves delivery even after the source ref is deleted.
                    completion = await self.db.get_task_completion(task["id"])
                    recorded_head = await self._completion_source(store, completion)
                    if (
                        recorded_head and is_valid_git_oid(recorded_head)
                        and await self.git.ais_ancestor(str(store), recorded_head, base)
                    ):
                        source = recorded_head
                key = (task["id"], source)
                if not source:
                    unavailable.add(task["id"])
                    continue
                contained_in_main = await self.git.ais_ancestor(str(store), source, base)
                if key in parked and not contained_in_main:
                    unavailable.add(task["id"])
                    continue
                if key in done:
                    unavailable.discard(task["id"])
                    continue
                member = {
                    "task_id": task["id"],
                    "source_sha": source,
                    "parent_task_id": task["parent_task_id"],
                }
                if contained_in_main:
                    unavailable.discard(task["id"])
                    manifest.append(member)
                    if len(manifest) >= policy.max_batch_size:
                        break
                    continue
                # Keep an independently reusable parent aggregate, with ordinary merge
                # commits. No parent CI, verifier task, or worker-owned branch mutation.
                parent_id = task["parent_task_id"]
                if parent_id:
                    parent_ref = (
                        "refs/heads/aq/development/parent/"
                        + hashlib.sha256(parent_id.encode()).hexdigest()[:16]
                        + "/"
                        + assembly_id
                    )
                    previous = parent_heads.get(parent_id)
                    parent_base = previous or base
                    await self.run_git(store, "checkout", "--detach", "--force", parent_base)
                else:
                    parent_ref, previous = None, None
                    await self.run_git(store, "checkout", "--detach", "--force", head)
                result = await self.git.arun_git_result(
                    [
                        "-c",
                        "user.name=Agent Queue",
                        "-c",
                        "user.email=aq@localhost",
                        "merge",
                        "--no-edit",
                        source,
                    ],
                    cwd=str(store),
                )
                if result.returncode == 0 and parent_ref:
                    aggregate_head = await self.run_git(store, "rev-parse", "HEAD")
                    await self.publish(
                        repo,
                        store,
                        parent_ref,
                        aggregate_head,
                        previous,
                        [member],
                        {"kind": "assembly", "conclusion": "not_run"},
                        "parent assembly",
                    )
                    parent_heads[parent_id] = aggregate_head
                    await self.run_git(store, "checkout", "--detach", "--force", head)
                    result = await self.git.arun_git_result(
                        [
                            "-c",
                            "user.name=Agent Queue",
                            "-c",
                            "user.email=aq@localhost",
                            "merge",
                            "--no-edit",
                            aggregate_head,
                        ],
                        cwd=str(store),
                    )
                if result.returncode:
                    await self.run_git(store, "merge", "--abort")
                    await self.run_git(store, "checkout", "--detach", "--force", head)
                    now = time.time()
                    await self.save(
                        {
                            "id": str(uuid4()),
                            "project_id": project_id,
                            "repository_id": repo.id,
                            "target_ref": target,
                            "expected_sha": base,
                            "prepared_sha": None,
                            "state": "parked",
                            "manifest": [member],
                            "evidence": {"kind": "merge_conflict", "detail": result.stdout[-4000:]},
                            "reason": "source conflict; independent work may continue",
                            "created_at": now,
                            "updated_at": now,
                        }
                    )
                    conflicts.append(member)
                    if parent_id:
                        blocked_parents.add(parent_id)
                    unavailable.add(task["id"])
                    continue
                head = await self.run_git(store, "rev-parse", "HEAD")
                unavailable.discard(task["id"])
                manifest.append(member)
                if len(manifest) >= policy.max_batch_size:
                    break
            await self.run_git(store, "checkout", "--detach", "--force", head)
            if not manifest:
                await self.reconcile_parked(repo, store, base)
                return {"outcome": "idle", "parked": conflicts}
            head = await self.run_git(store, "rev-parse", "HEAD")
            evidence, passed = await self.validate(store, policy)
            evidence["head_sha"] = head
            if await self.run_git(store, "rev-parse", "HEAD") != head or await self.run_git(
                store, "status", "--porcelain", "--untracked-files=no"
            ):
                raise ValueError("validation modified the candidate; refusing publication")
            # Retain the candidate remotely even if validation fails; no worker owns this ref.
            snapshot = (
                "refs/heads/aq/development/"
                + hashlib.sha256(project_id.encode()).hexdigest()[:12]
                + "/"
                + head
            )
            existing = await self.remote(store, snapshot)
            if existing not in {None, head}:
                raise ValueError("candidate snapshot identity conflicts")
            if existing is None:
                await self.publish(
                    repo, store, snapshot, head, None, manifest, evidence, "candidate preservation"
                )
            if not passed:
                now = time.time()
                await self.save(
                    {
                        "id": str(uuid4()),
                        "project_id": project_id,
                        "repository_id": repo.id,
                        "target_ref": target,
                        "expected_sha": base,
                        "prepared_sha": head,
                        "state": "parked",
                        "manifest": manifest,
                        "evidence": evidence,
                        "reason": "selected validation failed",
                        "created_at": now,
                        "updated_at": now,
                    }
                )
                await self.reconcile_parked(repo, store, base)
                return {"outcome": "parked", "head_sha": head, "evidence": evidence}
            result = await self.publish(
                repo, store, target, head, base, manifest, evidence, "development batch"
            )
            await self.reconcile_parked(
                repo, store, head if result["outcome"] == "delivered" else base
            )
            return result

    async def reconcile_parked(self, repo, store, accepted_head):
        """Dispatch only failures still unresolved after the whole batch was assembled.

        A later consolidation may contain an earlier conflicting source. Starting
        a worker mid-assembly spends tokens repairing work this batch already fixes.
        Parked rows are durable, so a subsequent sweep resumes dispatch after a crash.
        """
        history = await self.rows(repo.project_id)
        pending = {r["id"]: r for r in history if r["state"] == "parked" and r["manifest"]
                   and r["repository_id"] == repo.id}
        # Resolve repair-of-repair chains before dispatching any more work.
        # Each successful pass removes at least one row, so this is bounded.
        while pending:
            progress = False
            for identity, row in list(pending.items()):
                contained = True
                for member in row["manifest"]:
                    source = member.get("source_sha")
                    if not source or not await self.git.ais_ancestor(str(store), source, accepted_head):
                        contained = False
                        break
                proof = None if contained else await self._delivered_repair(
                    repo, store, accepted_head, row["manifest"], history,
                )
                if not contained and proof is None:
                    continue
                evidence = {**row["evidence"], **(
                    {"resolved_by_main_ancestry": accepted_head} if contained else
                    {"resolved_by_delivered_repair": proof}
                )}
                await self.change(identity, state="adopted", prepared_sha=accepted_head, evidence=evidence)
                history.append({**row, "state": "adopted", "prepared_sha": accepted_head,
                                "evidence": evidence, "updated_at": time.time()})
                del pending[identity]
                progress = True
            if not progress:
                break
        for row in pending.values():
            await self.ensure_repair(
                repo.project_id, repo.id, row["manifest"],
                row["prepared_sha"] or accepted_head, reason=row["reason"],
            )
        await self.reconcile_completion_sources(repo, store, history)

    async def _completion_source(self, store, completion):
        if completion is None or not completion.commits:
            return None
        reported = completion.commits[-1]
        if is_valid_git_oid(reported):
            return reported
        if not isinstance(reported, str) or not re.fullmatch(r"[0-9a-f]{7,39}", reported):
            return None
        try:
            return await self.run_git(
                store, "rev-parse", "--verify", "--end-of-options", reported + "^{commit}",
            )
        except GitError:
            return None

    async def reconcile_completion_sources(self, repo, store, history):
        """Bind unique abbreviated completion IDs to exact delivered revisions.

        Keep the original completion evidence intact. Readiness consumes this
        publisher proof, never a SQL prefix match that could accept ambiguity.
        """
        completions = {}
        for row in history:
            if (row["repository_id"] != repo.id
                    or row["target_ref"] != "refs/heads/" + repo.default_branch
                    or row["state"] not in {"delivered", "adopted"}):
                continue
            proofs = list(row["evidence"].get("completion_sources", []))
            changed = False
            for member in row["manifest"]:
                identity = member["task_id"]
                if identity not in completions:
                    completion = await self.db.get_task_completion(identity)
                    canonical = await self._completion_source(store, completion)
                    completions[identity] = completion, canonical
                completion, canonical = completions[identity]
                if (completion is None or not completion.commits or canonical is None
                        or canonical != member.get("source_sha")
                        or canonical == completion.commits[-1]):
                    continue
                proof = {"task_id": identity, "completion_id": completion.id,
                         "reported_sha": completion.commits[-1], "source_sha": canonical}
                if proof not in proofs:
                    proofs.append(proof)
                    changed = True
            if changed:
                await self.change(row["id"], evidence={**row["evidence"], "completion_sources": proofs})

    async def _delivered_repair(self, repo, store, accepted_head, manifest, history):
        """A passing resolution delivered to main can replace the original source.

        A cherry-pick or rewritten conflict resolution need not retain source
        ancestry. Its exact repair contract, completion and publication are
        the evidence; a task merely marked completed is insufficient.
        """
        identity = self._repair_identity(manifest)
        task = await self.db.get_task(identity)
        if (
            task is None or task.status != TaskStatus.COMPLETED
            or task.project_id != repo.project_id or task.repo_id != repo.id
            or task.branch_name != "aq/" + identity
        ):
            return None
        contract = await self.db.get_task_meta(identity, "development_repair_sources")
        if contract != manifest:
            # Compatibility for existing server-created repair tasks. Require
            # the exact generated source block, not just a repair-looking ID.
            sources = "\n".join(f"- {m['task_id']}: {m.get('source_sha')}" for m in manifest)
            if contract is not None or (
                f"The development batch parked these source revisions:\n{sources}\nCandidate/base: "
                not in task.description
            ):
                return None
        completion = await self.db.get_task_completion(identity)
        if completion is None or completion.outcome != "pass":
            return None
        source = await self._completion_source(store, completion)
        for delivery in history:
            if (
                delivery["state"] not in {"delivered", "adopted"}
                or delivery["repository_id"] != repo.id
                or delivery["target_ref"] != "refs/heads/" + repo.default_branch
                or float(delivery["created_at"]) < completion.completed_at
                or not any(m["task_id"] == identity and (
                    not completion.commits or (source and m.get("source_sha") == source)
                ) for m in delivery["manifest"])
            ):
                continue
            head = delivery.get("prepared_sha")
            if head and await self.git.ais_ancestor(str(store), head, accepted_head):
                return {"task_id": identity, "completion_id": completion.id,
                        "delivery_id": delivery["id"], "accepted_head": accepted_head}
        return None

    @staticmethod
    def _repair_identity(manifest):
        digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()[:20]
        return "development-repair-" + digest

    async def tick(self, now):
        async with self.db._engine.connect() as conn:
            rows = (
                (
                    await conn.execute(
                        select(projects.c.id, projects.c.hierarchical_integration_policy).where(
                            projects.c.hierarchical_integration_mode == "development",
                            projects.c.status == "ACTIVE",
                        )
                    )
                )
                .mappings()
                .all()
            )
        for row in rows:
            project_id = row["id"]
            if now < self.next_due.get(project_id, 0):
                continue
            policy = DevelopmentPolicy.model_validate(
                row["hierarchical_integration_policy"]
            ).checked()
            self.next_due[project_id] = now + policy.interval_seconds
            try:
                await self.preserve_stopped_owners(project_id)
                await self.sweep(project_id)
            except Exception:
                import logging

                logging.getLogger(__name__).exception(
                    "Development batch remains recoverable for %s", project_id
                )

    async def configure(self, project_id, policy, *, reason, operator_id):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from src.database.tables import (
            integration_branch_owners,
            integration_legacy_suppression,
            integration_promotion_intents,
        )

        policy = DevelopmentPolicy.model_validate(policy).checked()
        if not reason.strip():
            raise ValueError("configuration reason is required")
        project = await self.db.get_project(project_id)
        if project is None:
            raise ValueError("project not found")
        if project.integration_repository_id:
            repo = await self.db.get_repo(project.integration_repository_id)
        else:
            repositories = await self.db.list_repos(project_id)
            repository_url = project.repo_url
            if not repositories and not repository_url:
                # Initialized/local projects can gain an origin after
                # onboarding. Discover it from their registered checkout
                # when the operator explicitly enables delivery.
                workspace = await self.db.get_project_workspace_path(project_id)
                if workspace:
                    try:
                        repository_url = await self.run_git(workspace, "remote", "get-url", "origin")
                        if repository_url and ":" not in repository_url:
                            # Relative filesystem origins are relative to the
                            # source checkout, not the publisher's clone.
                            repository_url = str((Path(workspace) / Path(repository_url).expanduser()).resolve())
                    except GitError:
                        pass
            if not repositories and repository_url:
                from src.database.tables import repos

                identity = "development-" + hashlib.sha256(project_id.encode()).hexdigest()[:20]
                async with self.db._engine.begin() as conn:
                    await conn.execute(
                        pg_insert(repos)
                        .values(
                            id=identity,
                            project_id=project_id,
                            url=repository_url,
                            default_branch=project.repo_default_branch,
                            source_type="clone",
                            source_path="",
                            checkout_base_path="",
                        )
                        .on_conflict_do_nothing()
                    )
                repositories = await self.db.list_repos(project_id)
            if len(repositories) != 1:
                raise ValueError(
                    "designate a repository when the project has zero or multiple repositories"
                )
            repo = repositories[0]
        if repo is None or not repo.url:
            raise ValueError("project repository needs a remote URL")
        async with self.exclusion(repo.id), self.db.immediate() as conn:
            await self.db.lock_hierarchy_project(conn, project_id)
            # Old in-flight default-branch writes must settle before switching publishers.
            pending = await conn.scalar(
                select(integration_promotion_intents.c.id)
                .where(
                    integration_promotion_intents.c.repository_id == repo.id,
                    integration_promotion_intents.c.target_branch.in_(
                        [repo.default_branch, "refs/heads/" + repo.default_branch]
                    ),
                    integration_promotion_intents.c.state.not_in(
                        ["committed", "conflict", "superseded"]
                    ),
                )
                .limit(1)
            )
            owner = await conn.scalar(
                select(integration_branch_owners.c.id)
                .where(
                    integration_branch_owners.c.repository_id == repo.id,
                    integration_branch_owners.c.ref.in_(
                        [repo.default_branch, "refs/heads/" + repo.default_branch]
                    ),
                    integration_branch_owners.c.handoff_state != "released",
                )
                .limit(1)
            )
            if pending or owner:
                raise DevelopmentBusy("legacy default-branch mutation must be reconciled first")
            await conn.execute(
                update(projects)
                .where(projects.c.id == project_id)
                .values(
                    integration_repository_id=repo.id,
                    repo_url=project.repo_url or repo.url,
                    hierarchical_integration_mode="development",
                    hierarchical_integration_desired_mode="development",
                    hierarchical_integration_draining=False,
                    hierarchical_integration_generation=projects.c.hierarchical_integration_generation
                    + 1,
                    hierarchical_integration_policy=policy.model_dump(),
                )
            )
            now = time.time()
            suppression = {
                "project_id": project_id,
                "generation": project.hierarchical_integration_generation + 1,
                "merge_sweep_suppressed": True,
                "final_review_route_suppressed": True,
                "legacy_gate_creation_suppressed": True,
                "policy_snapshot": policy.model_dump(),
                "updated_at": now,
            }
            await conn.execute(
                pg_insert(integration_legacy_suppression)
                .values(**suppression)
                .on_conflict_do_update(index_elements=["project_id"], set_=suppression)
            )
            await conn.execute(
                insert(deliveries).values(
                    id=str(uuid4()),
                    project_id=project_id,
                    repository_id=repo.id,
                    target_ref="refs/heads/" + repo.default_branch,
                    state="adopted",
                    manifest=[],
                    evidence={
                        "kind": "configuration",
                        "operator_id": operator_id,
                        "policy": policy.model_dump(),
                    },
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
        self.next_due[project_id] = time.time() + policy.interval_seconds
        return {
            "outcome": "configured",
            "project_id": project_id,
            "repository_id": repo.id,
            "policy": policy.model_dump(),
        }

    async def cancel_preserving(self, operation_id, *, reason):
        from src.database.tables import integration_batches
        from src.database.tables import integration_branch_owners as owners
        from src.database.tables import integration_repair_operations as operations
        from src.database.tables import integration_repair_stages as stages

        if not reason.strip():
            raise ValueError("cancellation reason is required")
        async with self.db.immediate() as conn:
            operation = (
                (
                    await conn.execute(
                        select(operations).where(operations.c.id == operation_id).with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if operation is None:
                raise ValueError("operation not found")
            if operation["state"] == "completed":
                return {"outcome": "already_terminal", "operation_id": operation_id}
            delegates = list(
                (
                    await conn.execute(
                        select(stages.c.repair_task_id).where(
                            stages.c.operation_id == operation_id,
                            stages.c.repair_task_id.is_not(None),
                        )
                    )
                ).scalars()
            )
            if operation["verifier_task_id"]:
                delegates.append(operation["verifier_task_id"])
            delegates = list(dict.fromkeys(delegates))
            if operation["state"] == "cancelled":
                # Older cancellations omitted the verifier. Replaying must
                # retire those stranded tasks while retaining their audit rows.
                pending = await conn.scalar(
                    select(tasks.c.id).where(
                        tasks.c.id.in_(delegates),
                        tasks.c.status.not_in(["COMPLETED", "FAILED", "PAUSED"]),
                    ).limit(1)
                )
                if pending is None:
                    return {"outcome": "already_terminal", "operation_id": operation_id}
            retained = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(owners).where(owners.c.owner_id.in_([operation_id, *delegates]))
                    )
                ).mappings()
            ]
            delegate_sessions = list(
                (
                    await conn.execute(
                        select(sessions.c.id).where(sessions.c.task_id.in_(delegates))
                    )
                ).scalars()
            )
            attached_ids = list(
                set(delegate_sessions + [r["session_id"] for r in retained if r["session_id"]])
            )
            if attached_ids:
                live = await conn.scalar(
                    select(sessions.c.id)
                    .where(
                        sessions.c.id.in_(attached_ids),
                        (sessions.c.state != "stopped") | (sessions.c.desired_state != "stopped"),
                    )
                    .limit(1)
                )
                if live:
                    raise DevelopmentBusy("stop the current repair writer before cancellation")
                stopped = (
                    (
                        await conn.execute(
                            select(sessions)
                            .where(sessions.c.id.in_(attached_ids))
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .all()
                )
                if len(stopped) != len(set(attached_ids)) or self.confirm_stopped is None:
                    raise DevelopmentBusy("provider termination proof is unavailable")
                for session in stopped:
                    if not await self.confirm_stopped(dict(session)):
                        raise DevelopmentBusy("provider still has the repair writer")
            current_owners = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(owners)
                        .where(owners.c.owner_id.in_([operation_id, *delegates]))
                        .with_for_update()
                    )
                ).mappings()
            ]
            if sorted(current_owners, key=lambda r: r["id"]) != sorted(
                retained, key=lambda r: r["id"]
            ):
                raise DevelopmentBusy("writer changed during cancellation")
            now = time.time()
            for delegate in delegates:
                task = (
                    (
                        await conn.execute(
                            select(tasks).where(tasks.c.id == delegate).with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if task and task["status"] not in {"COMPLETED", "FAILED", "PAUSED"}:
                    await self.db._apply_transition(
                        conn,
                        delegate,
                        TaskStatus.PAUSED,
                        context="cancel_preserving",
                        force=True,
                        _manual_pause_control=True,
                        resume_after=None,
                    )
                    await self.db._upsert_meta(
                        delegate,
                        "manual_pause",
                        {
                            "sessions": [],
                            "status": task["status"],
                            "resume_after": None,
                            "agent_id": task["assigned_agent_id"],
                            "claim_epoch": task["claim_epoch"],
                            "cleanup_pending": False,
                            "reason": reason,
                        },
                        conn=conn,
                    )
            # Cancel scheduling but retain every attached fence/workspace as quarantine.
            # A detached reservation cannot issue Git writes; release only those rows.
            await conn.execute(
                update(owners)
                .where(
                    owners.c.owner_id.in_([operation_id, *delegates]),
                    owners.c.handoff_state == "reserved",
                    owners.c.session_id.is_(None),
                    owners.c.workspace_id.is_(None),
                )
                .values(handoff_state="released", updated_at=now)
            )
            await conn.execute(
                update(operations)
                .where(operations.c.id == operation_id)
                .values(state="cancelled", updated_at=now)
            )
            await conn.execute(
                update(stages)
                .where(
                    stages.c.operation_id == operation_id,
                    stages.c.state.not_in(["passed", "cancelled"]),
                )
                .values(state="cancelled", completed_at=now)
            )
            if operation["batch_id"]:
                await conn.execute(
                    update(integration_batches)
                    .where(integration_batches.c.id == operation["batch_id"])
                    .values(lifecycle="aborted", human_abort_reason=reason, updated_at=now)
                )
                # Preserve publication/repair refs and unresolved writes. Legacy lease cleanup
                # is intentionally left to the old recovery service; development uses its own
                # publisher exclusion and will not mutate those targets.
            if operation["parent_task_id"]:
                parent = (
                    (
                        await conn.execute(
                            select(tasks).where(tasks.c.id == operation["parent_task_id"])
                        )
                    )
                    .mappings()
                    .one()
                )
                project_id, repository_id = parent["project_id"], parent["repo_id"]
                target_ref = parent["branch_name"] or ""
            else:
                batch = (
                    (
                        await conn.execute(
                            select(integration_batches).where(
                                integration_batches.c.id == operation["batch_id"]
                            )
                        )
                    )
                    .mappings()
                    .one()
                )
                project_id, repository_id = batch["project_id"], batch["repository_id"]
                target_ref = batch["integration_branch"]
            await conn.execute(
                insert(deliveries).values(
                    id=str(uuid4()),
                    project_id=project_id,
                    repository_id=repository_id,
                    target_ref=target_ref,
                    state="cancelled",
                    manifest=[],
                    evidence={
                        "kind": "cancel_preserving",
                        "operation_id": operation_id,
                        "preserved_owners": [r["ref"] for r in retained],
                    },
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
            return {
                "outcome": "cancelled",
                "operation_id": operation_id,
                "preserved_owners": [r["ref"] for r in retained if r["session_id"]],
                "reason": reason,
            }

    async def preserve_stopped_owners(self, project_id):
        """Retain the old checkout intact and make a fresh workspace claim possible."""
        from src.database.tables import agents, task_session_attempts, workspaces
        from src.database.tables import integration_branch_owners as owners

        if self.confirm_stopped is None:
            return []
        project = await self.db.get_project(project_id)
        if project is None or project.hierarchical_integration_mode != "development":
            return []
        async with self.db._engine.connect() as conn:
            observed = [
                dict(r)
                for r in (
                    await conn.execute(
                        select(owners).where(
                            owners.c.repository_id == project.integration_repository_id,
                            owners.c.handoff_state.in_(["attached", "handoff_pending"]),
                            owners.c.session_id.is_not(None),
                            owners.c.workspace_id.is_not(None),
                        )
                    )
                ).mappings()
            ]
        preserved = []
        for owner in observed:
            result = None
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, project_id)
                session = (
                    (
                        await conn.execute(
                            select(sessions)
                            .where(sessions.c.id == owner["session_id"])
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                current = (
                    (
                        await conn.execute(
                            select(owners).where(owners.c.id == owner["id"]).with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                workspace = (
                    (
                        await conn.execute(
                            select(workspaces)
                            .where(workspaces.c.id == owner["workspace_id"])
                            .with_for_update()
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if (
                    not session
                    or not current
                    or not workspace
                    or dict(current) != owner
                    or session["state"] != "stopped"
                    or session["desired_state"] != "stopped"
                    or workspace["project_id"] != project_id
                    or session["work_dir"] != workspace["workspace_path"]
                    or not await self.confirm_stopped(dict(session))
                ):
                    continue
                detached = session["task_id"] is None
                if detached:
                    # A stop may have cleared the claim before releasing its branch.
                    # Prove the ended attempt and exclude successor users before
                    # preserving that checkout or repairing its agent definition.
                    ended = (await conn.execute(select(task_session_attempts.c.id).where(
                        task_session_attempts.c.session_id == session["id"],
                        task_session_attempts.c.task_id == owner["owner_id"],
                        task_session_attempts.c.project_id == project_id,
                        task_session_attempts.c.session_started_at == session["started_at"],
                        task_session_attempts.c.ended_at.is_not(None),
                    ).limit(1))).scalar_one_or_none()
                    task = (await conn.execute(select(tasks).where(
                        tasks.c.id == owner["owner_id"]
                    ).with_for_update())).mappings().one_or_none()
                    successor = (await conn.execute(select(sessions.c.id).where(
                        sessions.c.id != session["id"],
                        (sessions.c.work_dir == workspace["workspace_path"])
                        | (sessions.c.agent_id == session["agent_id"]),
                        (sessions.c.state != "stopped")
                        | (sessions.c.desired_state != "stopped"),
                    ).limit(1))).scalar_one_or_none()
                    if (not ended or not task or task["assigned_agent_id"]
                        or task["status"] not in {"READY", "PAUSED", "BLOCKED", "COMPLETED", "FAILED"}
                        or session["lifecycle"] != "pool" or successor
                        or workspace["locked_by_task_id"] or workspace["locked_by_agent_id"]):
                        continue
                    if session["agent_id"]:
                        await conn.execute(update(agents).where(
                            agents.c.id == session["agent_id"],
                            agents.c.state == "BUSY",
                            agents.c.current_task_id == owner["owner_id"],
                            agents.c.deleted_at.is_(None),
                        ).values(state="IDLE", current_task_id=None))
                elif (session["task_id"] != owner["owner_id"]
                      or workspace["locked_by_task_id"] != owner["owner_id"]):
                    continue
                # Retaining a checkout must not keep its branch checked out:
                # Git otherwise refuses the next worker in a different slot.
                # Detach at the existing HEAD; never reset or clean preserved work.
                await self.git._arun(["switch", "--detach"], cwd=workspace["workspace_path"])
                now = time.time()
                # Disable before unlocking: allocation cannot recycle this dirty checkout.
                await conn.execute(
                    update(workspaces)
                    .where(workspaces.c.id == workspace["id"])
                    .values(
                        enabled=False,
                        locked_by_task_id=None,
                        locked_by_agent_id=None,
                        locked_at=None,
                        lock_mode=None,
                    )
                )
                if session["task_id"]:
                    task = (
                        (
                            await conn.execute(
                                select(tasks)
                                .where(tasks.c.id == session["task_id"])
                                .with_for_update()
                            )
                        )
                        .mappings()
                        .one_or_none()
                    )
                    if task and task["status"] != "PAUSED":
                        state = TaskStatus(task["status"])
                        if state in {TaskStatus.IN_PROGRESS, TaskStatus.ASSIGNED}:
                            state = TaskStatus.READY
                        result = await self.db.release_claim(
                            session["id"],
                            task_status=state,
                            context="development_preserved_writer",
                            now=now,
                            conn=conn,
                            expected_task_id=session["task_id"],
                            expected_claim_epoch=session["last_claim_epoch"],
                            preserve_terminal_task=True,
                            release_workspace_lock=False,
                        )
                await conn.execute(
                    update(owners)
                    .where(owners.c.id == owner["id"])
                    .values(
                        handoff_state="released",
                        fence_token=owner["fence_token"] + 1,
                        session_id=None,
                        workspace_id=None,
                        updated_at=now,
                    )
                )
                await conn.execute(
                    insert(deliveries).values(
                        id=str(uuid4()),
                        project_id=project_id,
                        repository_id=project.integration_repository_id,
                        target_ref=owner["ref"],
                        state="cancelled",
                        manifest=[],
                        evidence={
                            "kind": "preserved_workspace",
                            "workspace_id": workspace["id"],
                            "path": workspace["workspace_path"],
                            "session_id": session["id"],
                            "instance_token": session["instance_token"],
                            "old_fence": owner["fence_token"],
                        },
                        reason="stopped writer preserved; fresh claims use a different workspace",
                        created_at=now,
                        updated_at=now,
                    )
                )
                preserved.append(workspace["id"])
            if result is not None:
                await self.db._after_release(result)
        return preserved

    async def ensure_repair(self, project_id, repository_id, manifest, candidate_sha, *, reason):
        """One ordinary resumable task per failed content set, with no wall-clock ladder."""
        from src.models import Task, TaskType

        identity = self._repair_identity(manifest)
        if await self.db.get_task(identity) is not None:
            return identity
        generation = 1
        for member in manifest:
            source_task = await self.db.get_task(member["task_id"])
            if source_task and source_task.id.startswith("development-repair-"):
                import re

                match = re.search(
                    r"Development repair generation: (\d+)", source_task.description or ""
                )
                generation = max(generation, (int(match.group(1)) if match else 1) + 1)
        if generation > 3:
            return None  # Keep the candidate parked for operator inspection; no unbounded repair chain.
        sources = "\n".join(f"- {m['task_id']}: {m.get('source_sha')}" for m in manifest)
        await self.db.create_task(
            Task(
                id=identity,
                project_id=project_id,
                repo_id=repository_id,
                title="Repair development integration: " + reason,
                description=(
                    f"Development repair generation: {generation}\nThe development batch parked these source revisions:\n{sources}\n"
                    f"Candidate/base: {candidate_sha}. Preserve their intended changes, resolve against current main, "
                    "and publish the repair on your own task branch. Ordinary merge commits are allowed. "
                    "Run focused local checks and close with actual evidence; no parent verifier or PR is required. "
                    "A passing close attests that every listed source revision is resolved; once your repair "
                    "is delivered to main, that delivery also satisfies those sources for their successors. "
                    "Do not push main. The development publisher will collect your branch. "
                    "This is one resumable repair task; queue and provider waits do not expire it."
                ),
                branch_name="aq/" + identity,
                status=TaskStatus.READY,
                task_type=TaskType.BUGFIX,
                max_retries=3,
            )
        )
        await self.db.set_task_meta(identity, "development_repair_sources", manifest)
        return identity
