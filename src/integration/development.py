"""Development delivery: Git manifests, short publication exclusion, and recoverable facts.

No synthetic review/CI receipts are written. Legacy episodes remain audit history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select, text, update

from src.database.queries.blocked_state import _development_delivery_pending, blocked_predicate
from src.database.tables import archived_tasks, projects, sessions, tasks
from src.database.tables import development_deliveries as deliveries
from src.git.manager import GitError, GitManager, is_valid_git_oid
from src.integration import development_validation as validation_outcomes
from src.integration.delegate_release import release_delegates_on
from src.integration.delivery_branches import (
    ASSEMBLY_PREFIX,
    TASK_BRANCH_PREFIX,
    branch_of,
    delete_branches,
    expired_task_branches,
    find_stale_branches,
    live_branch_references,
    released_integration_refs,
    remote_heads,
)
from src.integration.development_validation import run_check as run_validation_check
from src.integration.publishable_artifact import has_publishable_artifact
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Statuses :meth:`Database.archive_task` accepts.  Anything in the archive is
#: already in one of them, so an archived source has finished being worked on.
TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
)

#: Evidence key on a default-branch journal row recording the removal of the
#: refs that delivery made obsolete (:meth:`DevelopmentIntegration.
#: collect_delivered_branches`).  ``{"state": "pending"}`` arms it when the row
#: lands; ``complete`` / ``exhausted`` end it.  Rows journaled before the key
#: existed carry none and are left to ``aq doctor --check
#: git.stale_branches``.
BRANCH_CLEANUP_KEY = "branch_cleanup"
#: A cleanup that cannot confirm its deletes retries with backoff this often.
BRANCH_CLEANUP_MAX_ATTEMPTS = 8
BRANCH_CLEANUP_RETRY_SECONDS = 300.0
BRANCH_CLEANUP_RETRY_MAX_SECONDS = 6 * 3600.0
#: Journal rows cleaned per project per tick; a backlog drains over ticks.
BRANCH_CLEANUP_ROWS_PER_TICK = 10
PUBLISHER_SKIP_KEY = "development_publisher_skip"
#: Runs whose full evidence a deferral streak row keeps (the count is kept
#: for every run; older runs' output is dropped).
DEFERRAL_RUNS_KEPT = 5
#: Task metadata key holding what a repair's parked batch recorded.
REPAIR_EVIDENCE_KEY = "development_repair_evidence"
#: How much of a failure a repair description quotes.
REPAIR_TESTS_LISTED = 20
REPAIR_OUTPUT_TAIL_CHARS = 3000


def armed_for_branch_cleanup(evidence):
    """*evidence* with branch cleanup armed, for a row that just landed on main."""
    return {**evidence, BRANCH_CLEANUP_KEY: {"state": "pending", "attempts": 0}}


def _manifest_members(manifest):
    return [m for m in manifest or [] if isinstance(m, dict) and m.get("task_id")]


def _dependency_cycles(dependencies, task_ids):
    """Return strongly connected components that actually contain a cycle."""
    index = 0
    indices, lowlinks, stack, on_stack, cycles = {}, {}, [], set(), []

    def visit(task_id):
        nonlocal index
        indices[task_id] = lowlinks[task_id] = index
        index += 1
        stack.append(task_id)
        on_stack.add(task_id)
        for dependency_id in sorted(dependencies.get(task_id, set()) & task_ids):
            if dependency_id not in indices:
                visit(dependency_id)
                lowlinks[task_id] = min(lowlinks[task_id], lowlinks[dependency_id])
            elif dependency_id in on_stack:
                lowlinks[task_id] = min(lowlinks[task_id], indices[dependency_id])
        if lowlinks[task_id] != indices[task_id]:
            return
        component = set()
        while True:
            member = stack.pop()
            on_stack.remove(member)
            component.add(member)
            if member == task_id:
                break
        if len(component) > 1 or task_id in dependencies.get(task_id, set()):
            cycles.append(component)

    for task_id in sorted(task_ids):
        if task_id not in indices:
            visit(task_id)
    return cycles


@dataclass(frozen=True)
class ResolvedTask:
    """The publisher's view of a task, wherever the task currently lives.

    Archiving moves a terminal task out of ``tasks`` and into
    ``archived_tasks``.  A publisher that reads only the live table therefore
    sees a finished task as *missing*, which is not the same thing at all: it
    loses the task's project, its branch and — for a repair — the generation
    that bounds the repair chain.
    """

    task_id: str
    project_id: str
    repo_id: str | None
    status: str
    description: str
    branch_name: str | None
    archived: bool

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


class DevelopmentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")
    validation: str = "focused"
    commands: list[str] = Field(default_factory=list)
    #: Seconds a validation command may *run*.  Time it spends queued for a
    #: test slot is not charged here; ``slot_wait_seconds`` bounds that.
    timeout_seconds: int = Field(default=300, gt=0, le=3600)
    #: Seconds a validation command may wait for a test slot before the batch
    #: is deferred to the next tick (never parked, never repaired).
    slot_wait_seconds: int = Field(default=600, ge=0, le=3600)
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
    def __init__(
        self,
        db,
        *,
        data_dir,
        git: GitManager,
        confirm_stopped=None,
        owner_recovery: Any | None = None,
        job_client=None,
    ):
        self.db = db
        self.data_dir = Path(data_dir) / "development-integration"
        #: Bundles and the deletion log every branch delete writes first.
        self.backup_dir = Path(data_dir) / "backups" / "branch-deletions"
        self.git = git
        self.next_due = {}
        self._project_faults = {}
        self.confirm_stopped = confirm_stopped
        self.owner_recovery = owner_recovery
        self.job_client = job_client
        #: Poll durable completion; the runner owns all execution budgets.
        self.validation_poll_seconds = 1.0

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
            await self.git.acreate_checkout(repo.url, str(path), no_checkout=True)
        if await self.run_git(path, "remote", "get-url", "origin") != repo.url:
            raise ValueError("retained repository URL differs from configured repository")
        await self.git.afetch_origin(str(path), repository_url=repo.url, all_heads=True)
        return path

    async def remote(self, store, ref):
        await self.run_git(store, "check-ref-format", ref)
        from src.git.manager import RemoteRefState

        branch = ref.removeprefix("refs/heads/")
        observed = await self.git.als_remote_ref(str(store), branch)
        if observed.state is RemoteRefState.ERROR:
            raise GitError(observed.error or "remote head observation failed")
        return observed.oid

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

    async def _has_pending_work(self, project_id, repo, *, now):
        """Check durable work before opening the authenticated Git transport.

        Use the same delivery predicate as readiness, including its latest
        reported completion source. Branch cleanup is included only when its
        retry deadline has passed.
        """
        target = "refs/heads/" + repo.default_branch
        cleanup = deliveries.c.evidence[BRANCH_CLEANUP_KEY]
        async with self.db._engine.connect() as conn:
            journal_work = await conn.scalar(
                select(deliveries.c.id).where(
                    deliveries.c.project_id == project_id,
                    deliveries.c.repository_id == repo.id,
                    (
                        deliveries.c.state.in_(("prepared", "publishing", "parked"))
                        | (
                            deliveries.c.state.in_(("delivered", "adopted"))
                            & (deliveries.c.target_ref == target)
                            & (cleanup["state"].as_string() == "pending")
                            & (func.coalesce(cleanup["next_attempt_at"].as_float(), 0.0) <= now)
                        )
                    ),
                ).limit(1)
            )
            if journal_work is not None:
                return True
            candidate = await conn.scalar(
                select(tasks.c.id).where(
                    tasks.c.project_id == project_id,
                    tasks.c.status == TaskStatus.COMPLETED.value,
                    _development_delivery_pending(tasks),
                ).limit(1)
            )
            return candidate is not None

    async def rebind_foreign_repositories(self, project_id, repo):
        """Deliver this project's tasks that still name another project's repository.

        The sweep collects a project's tasks on its own repository, and every
        other project's publisher collects only that project's tasks, so a
        task whose ``repo_id`` names a repository of *another* project is
        collected by nobody -- and readiness, scoped the same way, counts it
        as delivered.  A project move used to leave exactly that behind
        (``fleet-meadow``, ``smart-orbit.10``); the move now rebinds, and this
        heals the rows it left.  Another repository of this same project is a
        deliberate binding and is left alone.  Each rebind is reported on the
        task, since it changes what the publisher will collect.
        """
        from src.database.tables import repos

        own = select(repos.c.id).where(repos.c.project_id == project_id)
        async with self.db._engine.begin() as conn:
            foreign = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.repo_id)
                    .where(
                        tasks.c.project_id == project_id,
                        tasks.c.repo_id.is_not(None),
                        tasks.c.repo_id.not_in(own),
                    )
                    .order_by(tasks.c.id)
                    .with_for_update()
                )
            ).all()
            if not foreign:
                return []
            ids = [row.id for row in foreign]
            await conn.execute(update(tasks).where(tasks.c.id.in_(ids)).values(repo_id=repo.id))
            flipped = await self.db.recompute_blocked(set(ids), conn=conn)
        await self.db.log_blocked_flips(flipped)
        for row in foreign:
            logger.warning(
                "development publisher rebound %s from %s to %s: the repository "
                "belongs to another project, so no publisher collected the task",
                row.id, row.repo_id, repo.id,
                extra={"project": project_id, "task": row.id},
            )
            try:
                await self.db.add_task_comment(
                    row.id,
                    (
                        f"Repository rebound from `{row.repo_id}` to `{repo.id}`: the task "
                        f"named another project's repository, so no publisher collected its "
                        f"branch. Development delivery for `{project_id}` now collects it "
                        f"from `{repo.url}`."
                    ),
                    author_kind="supervisor",
                    author_id="development-integration",
                )
            except Exception:  # the rebind is the fix; the note is a courtesy
                logger.debug("development publisher: rebind comment failed", exc_info=True)
        return ids

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
                evidence = row["evidence"] | {"reconciled_remote": actual}
                if row["target_ref"] == "refs/heads/" + repo.default_branch and row["manifest"]:
                    evidence = armed_for_branch_cleanup(evidence)
                await self.change(row["id"], state="delivered", evidence=evidence)
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

    async def validate(self, store, policy, *, project_id=None, operation_id=None, attempt=0):
        """Run the selected validation and classify it.

        ``evidence["conclusion"]`` is ``passed``, ``failed`` (tests ran and
        failed), ``infrastructure`` (nothing was verified: a timeout, no test
        slot, an outage, a kill, nothing collected) or ``not_run``.  The first
        infrastructure outcome ends the run: the batch will be deferred, so
        later commands would verify nothing either.
        """
        evidence = {"kind": "local", "validation": policy.validation, "checks": []}
        if policy.validation == "none":
            evidence["conclusion"] = "not_run"
            return evidence, True
        head = await self.run_git(store, "rev-parse", "HEAD")
        for index, command in enumerate(policy.commands):
            check = await run_validation_check(
                command,
                cwd=store,
                timeout_seconds=policy.timeout_seconds,
                slot_wait_seconds=policy.slot_wait_seconds,
                job_client=self.job_client, project_id=project_id,
                operation_id=operation_id, input_ref=head,
                idempotency_key=f"{operation_id}:{attempt}:{index}",
                poll_seconds=self.validation_poll_seconds,
            )
            evidence["checks"].append(check)
            if check["outcome"] == validation_outcomes.INFRASTRUCTURE:
                break
        conclusion = validation_outcomes.conclude(evidence["checks"])
        evidence["conclusion"] = conclusion
        evidence["failing_tests"] = validation_outcomes.failing_tests(evidence)
        passed = conclusion == validation_outcomes.PASSED
        return evidence, passed or policy.validation == "advisory"

    # -- validation that could not finish ----------------------------------

    @staticmethod
    def _open_deferral(history, repository_id):
        for row in reversed(history):
            evidence = row.get("evidence") or {}
            if (
                row["state"] == "cancelled"
                and row["repository_id"] == repository_id
                and evidence.get("kind") == validation_outcomes.DEFERRAL_KIND
                and evidence.get("open")
            ):
                return row
        return None

    async def _record_validation_deferral(self, repo, target, base, head, manifest, evidence):
        """Journal one infrastructure outcome on the project's open streak row.

        One ``cancelled`` row per streak, with an empty manifest: a deferral
        collects nothing and releases nothing.  It keeps the count, the time
        the streak began and the full evidence of its most recent runs, so
        ``aq integration status`` and doctor can say what kept failing
        without anyone reproducing it by hand.
        """
        now = time.time()
        failing = next(
            c for c in evidence["checks"]
            if c.get("outcome") == validation_outcomes.INFRASTRUCTURE
        )
        run = {
            "at": now,
            "reason": failing["infra_reason"],
            "detail": failing["detail"],
            "base_sha": base,
            "head_sha": head,
            "members": [m["task_id"] for m in _manifest_members(manifest)],
            "checks": evidence["checks"],
        }
        streak = self._open_deferral(await self.rows(repo.project_id), repo.id)
        if streak is None:
            row = {
                "id": str(uuid4()),
                "project_id": repo.project_id,
                "repository_id": repo.id,
                "target_ref": target,
                "expected_sha": base,
                "prepared_sha": head,
                "state": "cancelled",
                "manifest": [],
                "evidence": {
                    "kind": validation_outcomes.DEFERRAL_KIND,
                    "open": True,
                    "consecutive": 1,
                    "first_at": now,
                    "last_at": now,
                    "runs": [run],
                },
                "reason": "validation could not finish; batch deferred to the next tick",
                "created_at": now,
                "updated_at": now,
            }
            await self.save(row)
            identity, recorded = row["id"], row["evidence"]
        else:
            previous = streak["evidence"]
            recorded = {
                **previous,
                "consecutive": previous.get("consecutive", 0) + 1,
                "last_at": now,
                "runs": [*previous.get("runs", []), run][-DEFERRAL_RUNS_KEPT:],
            }
            identity = streak["id"]
            await self.change(
                identity, evidence=recorded, expected_sha=base, prepared_sha=head
            )
        consecutive = recorded["consecutive"]
        alerting = consecutive >= validation_outcomes.INFRA_ALERT_AFTER
        logger.log(
            logging.ERROR if alerting else logging.WARNING,
            "development validation for %s could not finish (%s: %s); batch of %d "
            "deferred, no repair filed (%d consecutive; evidence on journal row %s)",
            repo.project_id, run["reason"], run["detail"], len(run["members"]),
            consecutive, identity,
            extra={"project": repo.project_id, "deferral": identity,
                   "reason": run["reason"], "consecutive": consecutive},
        )
        if alerting:
            await self._alert_validation_infrastructure(repo, identity, recorded)
        return {"id": identity, "consecutive": consecutive, "reason": run["reason"]}

    async def _alert_validation_infrastructure(self, repo, identity, evidence):
        """Tell the project supervisor once per streak; the id makes it idempotent."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from src.database.tables import messages

        latest = evidence["runs"][-1]
        project_id = repo.project_id
        body = (
            f"Development validation for {project_id} has not finished "
            f"{evidence['consecutive']} times in a row; latest: {latest['reason']} "
            f"({latest['detail']}). The batch ({', '.join(latest['members']) or 'none'}) "
            "is deferred, not parked, and no repair was filed: nothing has shown the "
            "code to be broken. Check the validation environment (test database, test "
            "slots, timeout_seconds / slot_wait_seconds). Evidence: journal row "
            f"{identity} in `aq integration status {project_id}`."
        )
        async with self.db._engine.begin() as conn:
            await conn.execute(
                pg_insert(messages)
                .values(
                    id=f"msg-dev-validation-infra-{identity}",
                    project_id=project_id,
                    from_kind="system",
                    from_id="development-integration",
                    to_kind="session",
                    to_id=f"supervisor-{project_id}",
                    subject=f"Development validation for {project_id} keeps failing to finish",
                    body=body,
                    created_at=time.time(),
                    priority=50,
                    archive_after_inject=1,
                    body_kind="development_validation_infrastructure",
                )
                .on_conflict_do_nothing(index_elements=[messages.c.id])
            )

    async def _close_validation_deferrals(self, repo, conclusion, head):
        """A validation that reached a conclusion ends the deferral streak."""
        streak = self._open_deferral(await self.rows(repo.project_id), repo.id)
        if streak is None:
            return
        evidence = {
            **streak["evidence"],
            "open": False,
            "closed_at": time.time(),
            "closed_by": conclusion,
            "closed_head_sha": head,
        }
        await self.change(streak["id"], evidence=evidence)
        logger.info(
            "development validation for %s reached a conclusion (%s) after %d "
            "deferred run(s)",
            repo.project_id, conclusion, evidence.get("consecutive", 0),
            extra={"project": repo.project_id, "deferral": streak["id"]},
        )

    async def _release_unverified_parks(self, repo, history):
        """Release batches parked as validation failures that verified nothing.

        Before validation outcomes were classified, a timeout or an outage
        parked its batch as ``selected validation failed`` and filed a repair.
        Such a row holds its sources out of every later batch until that
        repair lands, for a failure no test ever showed.  Release it — the
        row keeps its evidence and says why — so the sources are validated
        again.  Returns *history* with the released rows updated.
        """
        released = []
        for row in history:
            if (
                row["state"] != "parked"
                or row["repository_id"] != repo.id
                or row.get("reason") != "selected validation failed"
                or (row.get("evidence") or {}).get("kind") != "local"
            ):
                continue
            conclusion = validation_outcomes.reclassify(row["evidence"])
            if conclusion != validation_outcomes.INFRASTRUCTURE:
                continue
            if await self.resolve_task(self._repair_identity(row["manifest"])) is not None:
                # A repair is already in flight; its delivery (or its own
                # ``blocks`` edges on the sources) settles this row as before.
                continue
            evidence = {
                **row["evidence"],
                "released": {"at": time.time(), "conclusion": conclusion},
            }
            await self.change(row["id"], state="cancelled", evidence=evidence)
            logger.warning(
                "development batch %s was parked for a validation that verified "
                "nothing; released for revalidation", row["id"],
                extra={"batch": row["id"], "project": repo.project_id},
            )
            released.append(row["id"])
        if not released:
            return history
        return await self.rows(repo.project_id)

    async def publish(
        self, repo, store, target_ref, head, expected, manifest, evidence, reason,
        *, arm_branch_cleanup=False,
    ):
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
        await self.git.apush_validated_ref(
            str(store), head, target_ref.removeprefix("refs/heads/"),
            expected_old_oid=expected or "0" * 40,
        )
        confirmed = await self.remote(store, target_ref)
        if confirmed != head:
            raise DevelopmentBusy("published ref could not be confirmed; journal retained")
        if arm_branch_cleanup:
            await self.change(
                row["id"], state="delivered", evidence=armed_for_branch_cleanup(evidence)
            )
        else:
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
                # The completion recorded below reports the adopted head, while
                # the manifest names each task by its branch head, so readiness
                # cannot match the two on its own. Bind them in the same row.
                recorded = {t["id"]: str(uuid4()) for t in selected if t["status"] != "COMPLETED"}
                sources = {member["task_id"]: member["source_sha"] for member in manifest}
                evidence = {
                    "kind": "operator_accepted",
                    "operator_id": operator_id,
                    "validation": "operator_decision",
                    "conclusion": "not_ci_attested",
                }
                if recorded:
                    evidence["completion_sources"] = [
                        self._completion_proof(task_id, completion_id, head_sha, sources[task_id])
                        for task_id, completion_id in sorted(recorded.items())
                    ]
                if target_ref == "refs/heads/" + repo.default_branch:
                    evidence = armed_for_branch_cleanup(evidence)
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
                        evidence=evidence,
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
                    if task_id in recorded:
                        await conn.execute(
                            insert(task_completion_records).values(
                                id=recorded[task_id],
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

    async def sweep(self, project_id, *, retry=False, recover_child_id=None):
        project = await self.db.get_project(project_id)
        if project is None or project.hierarchical_integration_mode != "development":
            raise ValueError("project is not in development mode")
        policy = DevelopmentPolicy.model_validate(project.hierarchical_integration_policy).checked()
        repo = await self.db.get_repo(project.integration_repository_id)
        await self.rebind_foreign_repositories(project_id, repo)
        await self.refresh_dependencies(project_id)
        async with self.exclusion(repo.id):
            if not (retry or recover_child_id) and not await self._has_pending_work(
                project_id, repo, now=time.time()
            ):
                return {"outcome": "idle", "parked": []}
            store = await self.store(repo)
            await self.reconcile(repo, store)
            target = "refs/heads/" + repo.default_branch
            base = await self.remote(store, target)
            if not base:
                raise ValueError("default branch does not exist")
            await self.run_git(store, "checkout", "--detach", "--force", base)
            history = await self._release_unverified_parks(repo, await self.rows(project_id))
            done = {
                (m["task_id"], m.get("source_sha"))
                for r in history
                if r["state"] in {"delivered", "adopted"}
                and r["repository_id"] == repo.id and r["target_ref"] == target
                for m in r["manifest"]
            }
            parked = (
                {
                    (m["task_id"], m.get("source_sha"))
                    for r in history
                    if r["state"] == "parked"
                    for m in r["manifest"]
                    if m["task_id"] != recover_child_id
                }
                if not retry
                else set()
            )
            manifest, conflicts = [], []
            async with self.db._engine.connect() as conn:
                # Recovering a repair must see its completed repair peers:
                # selecting only one vertex hides a repair dependency cycle.
                isolated_child = (
                    recover_child_id if recover_child_id and not
                    recover_child_id.startswith("development-repair-") else None
                )
                candidates = (
                    (
                        await conn.execute(
                            select(tasks)
                            .where(
                                tasks.c.project_id == project_id,
                                tasks.c.status == "COMPLETED",
                                (tasks.c.repo_id == repo.id) | tasks.c.repo_id.is_(None),
                                has_publishable_artifact(tasks.c.branch_name),
                                *([tasks.c.id == isolated_child] if isolated_child else []),
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
            candidate_by_id = {task["id"]: task for task in candidates}
            candidate_ids = set(candidate_by_id)
            repair_sources = await self.db.get_task_meta_bulk(
                sorted(task_id for task_id in candidate_ids
                       if task_id.startswith("development-repair-")),
                "development_repair_sources",
            )
            # A repair's source contract is a provenance edge, while the
            # source's blocks edge waits for that repair's delivery. Together
            # they form the repair/source cycle seen in production, even when
            # the blocking-edge graph alone is acyclic.
            repair_graph = {task_id: set(required) for task_id, required in dependencies.items()}
            for repair_id, contract in repair_sources.items():
                repair_graph.setdefault(repair_id, set()).update(
                    member["task_id"] for member in _manifest_members(contract)
                    if member["task_id"] in candidate_ids
                    and member["task_id"].startswith("development-repair-")
                )
            cycles = _dependency_cycles(repair_graph, candidate_ids)
            if recover_child_id and not isolated_child:
                # Keep --recover-child scoped to the named repair's cycle.
                # Unrelated completed work must not turn an explicit recovery
                # into a project-wide batch with sibling conflicts.
                selected = next(
                    (component for component in cycles if recover_child_id in component),
                    {recover_child_id},
                )
                candidates = [task for task in candidates if task["id"] in selected]
                candidate_by_id = {task["id"]: task for task in candidates}
                candidate_ids = set(candidate_by_id)
                dependencies = {
                    task_id: required for task_id, required in dependencies.items()
                    if task_id in candidate_ids
                }
                cycles = [component for component in cycles if component <= candidate_ids]
            superseded = {}  # old task id -> (new repair id, exact source sha)
            cycle_blocked = {}
            for component in cycles:
                newest = max(component, key=lambda task_id: (
                    candidate_by_id[task_id]["created_at"], task_id
                ))
                contract = repair_sources.get(newest)
                sources = {
                    member["task_id"]: member.get("source_sha")
                    for member in _manifest_members(contract)
                }
                older = component - {newest}
                if (
                    newest.startswith("development-repair-")
                    and older
                    and all(is_valid_git_oid(sources.get(task_id)) for task_id in older)
                ):
                    for task_id in older:
                        superseded[task_id] = (newest, sources[task_id])
                    # The replacement carries the older revisions. Preserve
                    # prerequisites outside this component and make every
                    # successor wait for the replacement's publication.
                    dependencies.setdefault(newest, set()).update(
                        dependency_id
                        for task_id in older
                        for dependency_id in dependencies.get(task_id, set())
                        if dependency_id not in component
                    )
                    for task_id, required in dependencies.items():
                        if task_id == newest:
                            required.difference_update(older)
                        elif required & older:
                            required.difference_update(older)
                            required.add(newest)
                else:
                    for task_id in component:
                        cycle_blocked[task_id] = sorted(component - {task_id})[0] if (
                            component - {task_id}
                        ) else task_id
            remaining = {
                task_id: task for task_id, task in candidate_by_id.items()
                if task_id not in superseded and task_id not in cycle_blocked
            }
            ordered = []
            while remaining:
                eligible = [
                    t
                    for t in remaining.values()
                    if not (dependencies.get(t["id"], set()) & remaining.keys())
                ]
                if not eligible:
                    # A cycle without a proven replacement is held with its
                    # actual cause; never guess that a source ref was deleted.
                    for task_id in remaining:
                        cycle_blocked[task_id] = sorted(
                            dependencies.get(task_id, set()) & remaining.keys()
                        )[0]
                    break
                for task in eligible:
                    ordered.append(task)
                    remaining.pop(task["id"])
            dependency_ids = set().union(*dependencies.values()) if dependencies else set()
            unavailable = {task_id for task_id, _source in parked}
            # An old parked receipt cannot make a branchless container require
            # publication. Include archived tasks because a dependency may
            # already have moved out of the live table.
            artifact_ids = (
                dependency_ids | unavailable
                | {task["parent_task_id"] for task in candidates if task["parent_task_id"]}
            ) - candidate_ids
            branch_by_id = {task["id"]: task["branch_name"] for task in candidates}
            if artifact_ids:
                async with self.db._engine.connect() as conn:
                    live = (
                        await conn.execute(
                            select(tasks.c.id, tasks.c.branch_name)
                            .where(tasks.c.id.in_(artifact_ids))
                        )
                    ).all()
                    branch_by_id.update(live)
                    missing = artifact_ids - branch_by_id.keys()
                    if missing:
                        archived = (
                            await conn.execute(
                                select(archived_tasks.c.id, archived_tasks.c.branch_name)
                                .where(archived_tasks.c.id.in_(missing))
                            )
                        ).all()
                        branch_by_id.update(archived)

            def requires_publication(task_id):
                # Missing tasks remain unavailable; only a known branchless
                # task is satisfied without a source.
                return task_id not in branch_by_id or has_publishable_artifact(
                    branch_by_id[task_id]
                )

            unavailable = {task_id for task_id in unavailable if requires_publication(task_id)}
            # A delivered source on the pinned target satisfies a dependency.
            # Recovery can leave newer parked/conflict rows for other attempts,
            # including rows later adopted by a repair. Those attempts cannot
            # revoke an earlier delivery of this task's source. A genuinely
            # newer completion is checked below against the pinned target.
            for dependency_id in tuple(unavailable):
                if await self._delivered_source(
                    store, history, dependency_id, base,
                    repository_id=repo.id,
                ):
                    unavailable.discard(dependency_id)
            # A dependency may no longer be a candidate at all: its worker
            # branch was cleaned up or its task was archived. Bind an already
            # contained source to the default ref so readiness can release
            # descendants without consulting that missing branch.
            for dependency_id in sorted(dependency_ids):
                if not requires_publication(dependency_id):
                    continue
                if dependency_id in candidate_ids or dependency_id in unavailable:
                    continue
                source = await self._delivered_source(
                    store, history, dependency_id, base, repository_id=repo.id
                )
                completion = await self.db.get_task_completion(dependency_id)
                latest = await self._completion_source(store, completion, history=history)
                if latest and latest != source and not await self.git.ais_ancestor(
                    str(store), latest, base
                ):
                    unavailable.add(dependency_id)
                    continue
                if source is None and latest and await self.git.ais_ancestor(
                    str(store), latest, base
                ):
                    source = latest
                if source is None:
                    unavailable.add(dependency_id)
                elif (dependency_id, source) not in done:
                    manifest.append({"task_id": dependency_id, "source_sha": source})
            head = base
            parent_heads = {}
            blocked_parents = set()
            processed, skipped = set(cycle_blocked), {
                task_id: ("dependency_cycle", dependency_id)
                for task_id, dependency_id in cycle_blocked.items()
            }
            for task_id, dependency_id in sorted(cycle_blocked.items()):
                logger.warning(
                    "development publisher: skipping %s: dependency cycle with %s",
                    task_id, dependency_id,
                )
            unavailable.update(cycle_blocked)
            assembly_id = uuid4().hex[:12]
            # Fetch above pins one remote snapshot. Do not make a network request
            # for every historical task branch on every sweep.
            fetched = await self.run_git(
                store, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/origin/"
            )
            source_heads = dict(line.split(" ", 1) for line in fetched.splitlines())
            for task in ordered:
                processed.add(task["id"])
                replacements = {
                    old_id: source_sha for old_id, (new_id, source_sha) in superseded.items()
                    if new_id == task["id"]
                }
                changed_source = next((
                    old_id for old_id, expected in sorted(replacements.items())
                    if source_heads.get(
                        "refs/remotes/origin/" + candidate_by_id[old_id]["branch_name"]
                        .removeprefix("refs/heads/")
                    ) not in {None, expected}
                ), None)
                if changed_source:
                    unavailable.update({task["id"], *replacements})
                    processed.update(replacements)
                    skipped[task["id"]] = ("dependency_cycle_source_changed", changed_source)
                    for old_id in replacements:
                        skipped[old_id] = ("dependency_cycle_source_changed", task["id"])
                    logger.warning(
                        "development publisher: skipping %s: dependency cycle with %s; "
                        "listed source changed",
                        task["id"], changed_source,
                    )
                    continue
                if (
                    task["parent_task_id"] in blocked_parents
                    and requires_publication(task["parent_task_id"])
                ):
                    unavailable.add(task["id"])
                    skipped[task["id"]] = ("parent_unavailable", task["parent_task_id"])
                    continue
                unavailable_dependencies = {
                    dependency_id
                    for dependency_id in dependencies.get(task["id"], set()) & unavailable
                    if requires_publication(dependency_id)
                }
                if unavailable_dependencies:
                    for dependency_id in sorted(unavailable_dependencies):
                        dependency_reason = skipped.get(dependency_id, (None, None))[0]
                        dependency_kind = (
                            "dependency_cycle" if dependency_reason == "dependency_cycle" else
                            "missing_ref" if dependency_reason == "missing_ref" else
                            "undelivered_dependency"
                        )
                        detail = {
                            "dependency_cycle": "dependency cycle with",
                            "missing_ref": "missing ref for dependency",
                            "undelivered_dependency": "undelivered dependency",
                        }[dependency_kind]
                        logger.warning(
                            "development publisher: skipping %s: %s %s",
                            task["id"], detail, dependency_id,
                            extra={
                                "candidate_task_id": task["id"],
                                "dependency_task_id": dependency_id,
                                "project": project_id,
                            },
                        )
                    unavailable.add(task["id"])
                    unavailable.update(replacements)
                    first_dependency = sorted(unavailable_dependencies)[0]
                    first_reason = skipped.get(first_dependency, (None, None))[0]
                    skipped[task["id"]] = (
                        "dependency_cycle" if first_reason == "dependency_cycle" else
                        "missing_ref" if first_reason == "missing_ref" else
                        "undelivered_dependency", first_dependency,
                    )
                    continue
                source = source_heads.get(
                    "refs/remotes/origin/" + task["branch_name"].removeprefix("refs/heads/")
                )
                if not source:
                    # Branch cleanup after merge must not strand every later
                    # batch. A durable completion plus default-branch ancestry
                    # proves delivery even after the source ref is deleted.
                    completion = await self.db.get_task_completion(task["id"])
                    recorded_head = await self._completion_source(
                        store, completion, history=history
                    )
                    if (
                        recorded_head and is_valid_git_oid(recorded_head)
                        and await self.git.ais_ancestor(str(store), recorded_head, base)
                    ):
                        source = recorded_head
                if not source:
                    # A delivered receipt is a source fact, independent of a
                    # worker's completion payload. In particular, Codex and
                    # OpenCode both legitimately close with ``commits: []``.
                    # Prefer the newest delivered/adopted receipt whose exact
                    # member is already on this sweep's pinned base.
                    source = await self._delivered_source(
                        store, history, task["id"], base, repository_id=repo.id
                    )
                key = (task["id"], source)
                if not source:
                    was_parked = task["id"] in unavailable
                    unavailable.add(task["id"])
                    unavailable.update(replacements)
                    skipped[task["id"]] = (
                        "source_parked" if was_parked else "missing_ref",
                        task["id"],
                    )
                    continue
                contained_in_main = await self.git.ais_ancestor(str(store), source, base)
                if key in parked and not contained_in_main:
                    unavailable.add(task["id"])
                    unavailable.update(replacements)
                    skipped[task["id"]] = ("source_parked", None)
                    continue
                if key in done and contained_in_main and all(
                    (old_id, old_sha) in done for old_id, old_sha in replacements.items()
                ):
                    unavailable.discard(task["id"])
                    unavailable.difference_update(replacements)
                    processed.update(replacements)
                    continue
                member = {
                    "task_id": task["id"],
                    "source_sha": source,
                    "parent_task_id": task["parent_task_id"],
                }
                if contained_in_main:
                    unavailable.discard(task["id"])
                    manifest.append(member)
                    for old_id, old_sha in sorted(replacements.items()):
                        manifest.append({"task_id": old_id, "source_sha": old_sha,
                                         "superseded_by": task["id"]})
                        unavailable.discard(old_id)
                        processed.add(old_id)
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
                    unavailable.update(replacements)
                    skipped[task["id"]] = ("merge_conflict", None)
                    continue
                head = await self.run_git(store, "rev-parse", "HEAD")
                unavailable.discard(task["id"])
                manifest.append(member)
                for old_id, old_sha in sorted(replacements.items()):
                    manifest.append({"task_id": old_id, "source_sha": old_sha,
                                     "superseded_by": task["id"]})
                    unavailable.discard(old_id)
                    processed.add(old_id)
                if len(manifest) >= policy.max_batch_size:
                    break
            await self._record_candidate_skips(processed, skipped)
            await self.run_git(store, "checkout", "--detach", "--force", head)
            if not manifest:
                await self.reconcile_parked(repo, store, base)
                return {"outcome": "idle", "parked": conflicts}
            head = await self.run_git(store, "rev-parse", "HEAD")
            operation = "development-" + hashlib.sha256(
                (repo.id + head + policy.model_dump_json()).encode()
            ).hexdigest()
            deferral = self._open_deferral(history, repo.id)
            attempt = (deferral or {}).get("evidence", {}).get("consecutive", 0)
            evidence, passed = await self.validate(
                store, policy, project_id=project_id, operation_id=operation, attempt=attempt
            )
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
            if not passed and evidence["conclusion"] == validation_outcomes.INFRASTRUCTURE:
                # Nothing was verified, so nothing failed: no park, no repair.
                # The members stay eligible and the next tick validates again.
                deferral = await self._record_validation_deferral(
                    repo, target, base, head, manifest, evidence
                )
                await self.reconcile_parked(repo, store, base)
                return {
                    "outcome": "deferred",
                    "reason": deferral["reason"],
                    "head_sha": head,
                    "evidence": evidence,
                    "deferral": deferral,
                }
            await self._close_validation_deferrals(repo, evidence["conclusion"], head)
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
            evidence = await self._bind_commitsless_completion_sources(evidence, manifest)
            result = await self.publish(
                repo, store, target, head, base, manifest, evidence, "development batch",
                arm_branch_cleanup=True,
            )
            await self.reconcile_parked(
                repo, store, head if result["outcome"] == "delivered" else base
            )
            return result

    async def _record_candidate_skips(self, processed, skipped):
        """Keep consecutive skip evidence across ticks and daemon restarts."""
        previous = await self.db.get_task_meta_bulk(sorted(processed), PUBLISHER_SKIP_KEY)
        now = time.time()
        for task_id in sorted(processed):
            reason = skipped.get(task_id)
            old = previous.get(task_id)
            if reason is None:
                if old is not None:
                    await self.db.delete_task_meta(task_id, PUBLISHER_SKIP_KEY)
                continue
            kind, dependency_id = reason
            same = isinstance(old, dict) and (
                old.get("reason") == kind and old.get("dependency_id") == dependency_id
            )
            await self.db.set_task_meta(task_id, PUBLISHER_SKIP_KEY, {
                "reason": kind,
                "dependency_id": dependency_id,
                "consecutive_ticks": old.get("consecutive_ticks", 0) + 1 if same else 1,
                "first_skipped_at": old.get("first_skipped_at", now) if same else now,
                "last_skipped_at": now,
            })

    async def recover_child(self, project_id, child_id, *, retry=False):
        """Run a supervised sweep and prove the named child's delivery."""
        task = await self.db.get_task(child_id)
        if (
            task is None or task.project_id != project_id
            or task.status != TaskStatus.COMPLETED or not task.branch_name
        ):
            raise ValueError(f"{child_id} is not a completed source task in {project_id}")
        # Recovery is an explicit retry of this child. Selecting only its branch
        # lets a sibling's parent-assembly conflict be handled independently.
        result = await self.sweep(project_id, retry=retry, recover_child_id=child_id)
        project = await self.db.get_project(project_id)
        repo = await self.db.get_repo(project.integration_repository_id)
        store = await self.store(repo)
        target = "refs/heads/" + repo.default_branch
        base = await self.remote(store, target)
        history = await self.rows(project_id)
        source = await self._delivered_source(
            store, history, child_id, base,
            repository_id=repo.id,
        )
        bound_to_main = any(
            row["state"] in {"delivered", "adopted"}
            and row["repository_id"] == repo.id
            and row["target_ref"] == target
            and any(
                member["task_id"] == child_id and member.get("source_sha") == source
                for member in _manifest_members(row["manifest"])
            )
            for row in history
        )
        completion = await self.db.get_task_completion(child_id)
        latest = await self._completion_source(store, completion, history=history)
        branch = await self.remote(
            store, "refs/heads/" + task.branch_name.removeprefix("refs/heads/")
        )
        if (source is None or not bound_to_main or (latest and latest != source)
                or (branch and branch != source)):
            skip = await self.db.get_task_meta(child_id, PUBLISHER_SKIP_KEY)
            if result.get("outcome") == "deferred":
                skip = (
                    f"validation could not finish ({result['reason']}); the batch is "
                    "deferred to the next tick, not parked"
                )
            raise ValueError(
                f"{child_id} remains unpublished after sweep: "
                f"{skip or 'no receipt for the current source'}"
            )
        sibling_conflicts = []
        if task.parent_task_id:
            for row in history:
                if row["state"] != "parked" or "conflict" not in row["reason"]:
                    continue
                for member in _manifest_members(row["manifest"]):
                    sibling_id = member["task_id"]
                    if sibling_id == child_id:
                        continue
                    sibling = await self.db.get_task(sibling_id)
                    if sibling and sibling.parent_task_id == task.parent_task_id:
                        sibling_conflicts.append(sibling_id)
        return {
            **result, "recovered_task_id": child_id, "source_sha": source,
            "sibling_conflicts": sorted(set(sibling_conflicts)),
        }

    async def reconcile_parked(self, repo, store, accepted_head):
        """Dispatch only failures still unresolved after the whole batch was assembled.

        A later consolidation may contain an earlier conflicting source. Starting
        a worker mid-assembly spends tokens repairing work this batch already fixes.
        Parked rows are durable, so a subsequent sweep resumes dispatch after a crash.
        """
        history = await self._release_unverified_parks(repo, await self.rows(repo.project_id))
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
                evidence = armed_for_branch_cleanup({**row["evidence"], **(
                    {"resolved_by_main_ancestry": accepted_head} if contained else
                    {"resolved_by_delivered_repair": proof}
                )})
                await self.change(identity, state="adopted", prepared_sha=accepted_head, evidence=evidence)
                history.append({**row, "state": "adopted", "prepared_sha": accepted_head,
                                "evidence": evidence, "updated_at": time.time()})
                del pending[identity]
                progress = True
            if not progress:
                break
        for row in pending.values():
            # Each parked row is dispatched on its own.  A row the publisher
            # cannot resolve records a named diagnostic and the sweep moves to
            # the next one; it never takes the remaining rows, the rest of the
            # project, or the other projects down with it.
            diagnostics = []
            try:
                await self.ensure_repair(
                    repo.project_id, repo.id, row["manifest"],
                    row["prepared_sha"] or accepted_head, reason=row["reason"],
                    diagnostics=diagnostics, parked=row,
                )
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                diagnostics.append(
                    {
                        "kind": "repair_dispatch_failed",
                        "task_ids": sorted(m["task_id"] for m in row["manifest"]),
                        "detail": f"{type(exc).__name__}: {exc}",
                    }
                )
            await self._record_batch_diagnostic(row, diagnostics)
        await self.reconcile_completion_sources(repo, store, history)

    async def _completion_source(self, store, completion, *, history=()):
        """Resolve a worker-reported source or its durable delivery binding."""
        if completion is None:
            return None
        if completion.commits:
            reported = completion.commits[-1]
            if is_valid_git_oid(reported):
                return reported
            if isinstance(reported, str) and re.fullmatch(r"[0-9a-f]{7,39}", reported):
                try:
                    return await self.run_git(
                        store,
                        "rev-parse",
                        "--verify",
                        "--end-of-options",
                        reported + "^{commit}",
                    )
                except GitError:
                    pass
        # A delivery receipt binds the exact completion record to the source
        # the publisher actually delivered. It is essential for legitimate
        # commits-less closes after branch cleanup has deleted the source ref.
        for row in reversed(history):
            if row["state"] not in {"delivered", "adopted"}:
                continue
            for proof in (row.get("evidence") or {}).get("completion_sources", []):
                if (
                    proof.get("task_id") == completion.task_id
                    and proof.get("completion_id") == completion.id
                    and is_valid_git_oid(proof.get("source_sha"))
                ):
                    return proof["source_sha"]
        return None

    async def _delivered_source(self, store, history, task_id, base, *, repository_id):
        """Return a receipted source contained in or superseded on *base*.

        A candidate or parent assembly may have been delivered to a temporary
        ref and later merged into the target. Its manifest and target ancestry
        remain proof after normal branch cleanup removes the source ref.
        """
        for row in reversed(history):
            if (
                row["state"] not in {"delivered", "adopted"}
                or row["repository_id"] != repository_id
            ):
                continue
            for member in reversed(_manifest_members(row["manifest"])):
                source = member.get("source_sha")
                if (
                    member["task_id"] == task_id
                    and is_valid_git_oid(source)
                    and (
                        await self.git.ais_ancestor(str(store), source, base)
                        or (
                            member.get("superseded_by")
                            and row["target_ref"].startswith("refs/heads/")
                            and is_valid_git_oid(row.get("prepared_sha"))
                            and await self.git.ais_ancestor(
                                str(store), row["prepared_sha"], base
                            )
                        )
                    )
                ):
                    return source
        return None

    @staticmethod
    def _completion_proof(task_id, completion_id, reported_sha, source_sha):
        """One ``completion_sources`` entry, the shape readiness matches on."""
        return {"task_id": task_id, "completion_id": completion_id,
                "reported_sha": reported_sha, "source_sha": source_sha}

    async def _bind_commitsless_completion_sources(self, evidence, manifest):
        """Bind empty worker close payloads before their delivery is recorded.

        The publisher, unlike an eventual branch cleanup, still holds the
        exact manifest source at this point. Store that fact on the delivered
        receipt rather than inventing commits in the worker's immutable close.
        """
        proofs = list(evidence.get("completion_sources", []))
        for member in _manifest_members(manifest):
            source = member.get("source_sha")
            if not is_valid_git_oid(source):
                continue
            completion = await self.db.get_task_completion(member["task_id"])
            if completion is None or completion.commits:
                continue
            proof = self._completion_proof(member["task_id"], completion.id, None, source)
            if proof not in proofs:
                proofs.append(proof)
        return {**evidence, **({"completion_sources": proofs} if proofs else {})}

    async def reconcile_completion_sources(self, repo, store, history):
        """Bind completions to delivered revisions the manifest does not name.

        Three cases: a unique abbreviated completion ID that Git resolves to
        the member's source, a completion that reports the row's own prepared
        head (which :meth:`adopt` writes), and a commits-less completion. The
        latter is bound directly to the publisher's delivered member so a
        later branch cleanup cannot make the close unresolvable.

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
                    canonical = await self._completion_source(store, completion, history=history)
                    completions[identity] = completion, canonical
                completion, canonical = completions[identity]
                source = member.get("source_sha")
                if completion is None or not is_valid_git_oid(source):
                    continue
                reported = completion.commits[-1] if completion.commits else None
                if not completion.commits:
                    # There is no worker-reported source to compare. The
                    # delivered manifest is the authoritative binding.
                    pass
                elif canonical == source:
                    if canonical == reported:
                        continue  # The manifest itself names this close.
                elif canonical != row["prepared_sha"]:
                    continue
                proof = self._completion_proof(
                    identity, completion.id, reported, source
                )
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
        # Through the archive as well: a repair that completed and was then
        # archived is still the proof that resolves its parked row.  Reading
        # only ``tasks`` made the publisher re-file an archived repair as a
        # fresh READY task on every tick.
        task = await self.resolve_task(identity)
        if (
            task is None or task.status != TaskStatus.COMPLETED.value
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
    def _name_diagnostic(diagnostics, *, kind, task_ids, detail):
        """Name a batch-scoped fault instead of raising it.

        The sweep visits every parked batch of every development project.  One
        batch that cannot be resolved is a fact about that batch; turning it
        into an exception starved the other twenty-four parked agent-queue
        batches and wrote a rich traceback every five minutes for months.
        """
        entry = {"kind": kind, "task_ids": sorted(task_ids), "detail": detail}
        if diagnostics is None:
            logger.warning(
                "development publisher: %s (%s)", detail, kind,
                extra={"diagnostic": kind, "task_ids": entry["task_ids"]},
            )
        else:
            diagnostics.append(entry)
        return entry

    async def _record_batch_diagnostic(self, row, diagnostics):
        """Persist at most one diagnostic per batch, and log only on change.

        ``consecutive_ticks`` is what makes a stall visible to
        ``aq doctor --check integration.development_publisher_stalled`` without
        the publisher having to keep process-local state across restarts.
        """
        evidence = row.get("evidence") or {}
        previous = evidence.get("publisher_diagnostic")
        if not diagnostics:
            if previous is None:
                return
            evidence = {k: v for k, v in evidence.items() if k != "publisher_diagnostic"}
            await self.change(row["id"], evidence=evidence)
            logger.info(
                "development batch %s recovered from %s", row["id"], previous.get("kind"),
                extra={"batch": row["id"], "diagnostic": previous.get("kind")},
            )
            return
        current = diagnostics[0]
        now = time.time()
        unchanged = (
            previous is not None
            and previous.get("kind") == current["kind"]
            and previous.get("detail") == current["detail"]
        )
        entry = {
            **current,
            "batch_id": row["id"],
            "first_failed_at": previous.get("first_failed_at", now) if unchanged else now,
            "last_failed_at": now,
            "consecutive_ticks": (previous.get("consecutive_ticks", 0) + 1) if unchanged else 1,
        }
        if len(diagnostics) > 1:
            entry["also"] = diagnostics[1:]
        await self.change(row["id"], evidence={**evidence, "publisher_diagnostic": entry})
        if not unchanged:
            # One line per batch per state change.  A repeat of the same fault
            # only bumps the counter above; it must not reach the log again.
            logger.warning(
                "development batch %s parked on %s: %s",
                row["id"], current["kind"], current["detail"],
                extra={"batch": row["id"], "diagnostic": current["kind"],
                       "project": row["project_id"]},
            )

    async def resolve_task(self, task_id):
        """Return *task_id* from the live table, else from the archive, else ``None``.

        Every publisher read of a manifest source goes through here.  Reading
        ``tasks`` alone is what wedged the agent-queue batch: an archived
        generation-3 repair read back as missing, reset the repair generation
        to 1, and raised ``repair source ... is not in project`` out of the
        whole sweep on every tick.
        """
        task = await self.db.get_task(task_id)
        if task is not None:
            status = task.status.value if hasattr(task.status, "value") else str(task.status)
            return ResolvedTask(
                task_id=task_id,
                project_id=task.project_id,
                repo_id=task.repo_id,
                status=status,
                description=task.description or "",
                branch_name=task.branch_name,
                archived=False,
            )
        row = await self.db.get_archived_task(task_id)
        if row is None:
            return None
        return ResolvedTask(
            task_id=task_id,
            project_id=row["project_id"],
            repo_id=row.get("repo_id"),
            status=row["status"],
            description=row.get("description") or "",
            branch_name=row.get("branch_name"),
            archived=True,
        )

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
            # Policy validation is guarded too, and arms the deadline itself:
            # a project whose stored policy no longer validates must neither
            # stop the fleet's remaining projects nor spin on every 5s cycle
            # because no deadline was ever set for it.
            try:
                policy = DevelopmentPolicy.model_validate(
                    row["hierarchical_integration_policy"]
                ).checked()
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                self.next_due[project_id] = now + DevelopmentPolicy().interval_seconds
                self._note_project_fault(project_id, exc)
                continue
            self.next_due[project_id] = now + policy.interval_seconds
            try:
                await self.preserve_stopped_owners(project_id)
                await self.sweep(project_id)
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                self._note_project_fault(project_id, exc)
            else:
                self._note_project_fault(project_id, None)
            # Its own isolation: a wedged batch must not keep delivered
            # branches around, and a cleanup fault must not look like a
            # publisher fault.  Failed deletes are recorded on the row and
            # retried from there; only an unexpected error reaches here.
            try:
                await self.collect_delivered_branches(project_id, now=now)
            except DevelopmentBusy:
                pass
            except Exception as exc:  # noqa: BLE001 - isolation is the point
                logger.warning(
                    "development branch cleanup failed for %s: %s: %s",
                    project_id, type(exc).__name__, exc,
                    extra={"project": project_id, "fault": type(exc).__name__},
                )

    def _note_project_fault(self, project_id, exc):
        """One structured warning per project per state change.

        The previous handler called ``logging.exception`` on every failing
        tick.  With a five-minute interval and a permanently wedged batch that
        is a multi-hundred-line rich traceback twelve times an hour; it grew
        daemon.log past eleven million lines.  The fault is a *state*, so it
        is logged when the state changes and counted the rest of the time.
        """
        faults = self._project_faults
        if exc is None:
            previous = faults.pop(project_id, None)
            if previous is not None:
                logger.info(
                    "development publisher recovered for %s after %d failing tick(s)",
                    project_id, previous["ticks"],
                    extra={"project": project_id},
                )
            return
        signature = f"{type(exc).__name__}: {exc}"
        previous = faults.get(project_id)
        if previous is not None and previous["signature"] == signature:
            previous["ticks"] += 1
            return
        faults[project_id] = {"signature": signature, "ticks": 1}
        logger.warning(
            "development publisher tick failed for %s: %s (batch state is durable; "
            "the next tick retries)",
            project_id, signature,
            extra={"project": project_id, "fault": type(exc).__name__},
        )

    # -- branches a delivery made obsolete --------------------------------

    async def collect_delivered_branches(self, project_id, *, now=None):
        """Delete from origin the refs that confirmed main deliveries made obsolete.

        Runs for each default-branch journal row armed with
        ``branch_cleanup: pending`` (armed when the row lands: published,
        reconciled, resolved from parked, or adopted).  For each row it deletes

        * every member task's branch, when its remote head is the delivered
          revision or is on the default branch, plus the ``-wip`` sibling
          when that one is on the default branch;
        * every candidate or parent assembly carrying one of the row's
          members, once *all* the members it carries are delivered — so a
          superseded candidate of a batch that parked goes too; and
        * for a parked batch that its sources resolved on their own, the
          branch of its repair when that repair ended FAILED or BLOCKED.
          The source worker's branch is deleted only as a delivered member.

        Nothing :func:`live_branch_references` holds is touched, and each
        delete is a lease on the observed head.  What was deleted, kept (with
        why) or already missing is recorded on the row; unconfirmed deletes
        retry with backoff up to ``BRANCH_CLEANUP_MAX_ATTEMPTS``.
        """
        now = time.time() if now is None else now
        project = await self.db.get_project(project_id)
        if (
            project is None
            or project.hierarchical_integration_mode != "development"
            or not project.integration_repository_id
        ):
            return {"outcome": "idle", "rows": []}
        repo = await self.db.get_repo(project.integration_repository_id)
        if repo is None:
            return {"outcome": "idle", "rows": []}
        target = "refs/heads/" + repo.default_branch

        def due(row):
            record = (row["evidence"] or {}).get(BRANCH_CLEANUP_KEY)
            return (
                row["repository_id"] == repo.id
                and row["target_ref"] == target
                and row["state"] in {"delivered", "adopted"}
                and isinstance(record, dict)
                and record.get("state") == "pending"
                and float(record.get("next_attempt_at") or 0) <= now
            )

        # Cheap and lock-free: most ticks have nothing armed.
        if not any(due(row) for row in await self.rows(project_id)):
            return {"outcome": "idle", "rows": []}
        async with self.exclusion(repo.id):
            history = await self.rows(project_id)
            pending = [row for row in history if due(row)][:BRANCH_CLEANUP_ROWS_PER_TICK]
            if not pending:
                return {"outcome": "idle", "rows": []}
            try:
                store = await self.store(repo)
                heads = await remote_heads(self.run_git, store)
                async with self.db._engine.connect() as conn:
                    holds = await live_branch_references(conn)
                plans = {
                    row["id"]: await self._plan_branch_cleanup(
                        repo, store, row, history, heads, holds
                    )
                    for row in pending
                }
                targets = {
                    branch: {"head": entry["head"], "reason": entry["reason"]}
                    for plan in plans.values()
                    for branch, entry in plan["delete"].items()
                }
                deletion = await delete_branches(
                    self.git, self.run_git, store, targets,
                    default_branch=repo.default_branch,
                    main_head=heads.get(repo.default_branch),
                    backup_dir=self.backup_dir, repository_id=repo.id, now=now,
                )
            except Exception as exc:  # noqa: BLE001 - one attempt, recorded, retried
                error = f"{type(exc).__name__}: {exc}"
                return {
                    "outcome": "retry",
                    "rows": [
                        await self._record_branch_cleanup(row, now, error=error)
                        for row in pending
                    ],
                }
            return {
                "outcome": "collected",
                "rows": [
                    await self._record_branch_cleanup(
                        row, now, plan=plans[row["id"]], deletion=deletion,
                    )
                    for row in pending
                ],
            }

    async def _plan_branch_cleanup(self, repo, store, row, history, heads, holds):
        """Decide what *row*'s landing lets go of.  Reads only; deletes nothing."""
        target = "refs/heads/" + repo.default_branch
        main_head = heads.get(repo.default_branch)
        delete, kept, missing = {}, [], []
        landed_by = f"delivered by development batch {row['id']}"

        async def on_main(head):
            return bool(main_head) and bool(
                await self.git.ais_ancestor(str(store), head, main_head)
            )

        async def consider(branch, *, kind, delivered=None, quiet=False):
            """Delete *branch* when it is still at *delivered* or is on main."""
            if branch is None or branch in delete:
                return
            if (
                not branch.startswith(TASK_BRANCH_PREFIX)
                or branch == repo.default_branch
                or (kind != "assembly" and branch.startswith(ASSEMBLY_PREFIX))
            ):
                if not quiet:
                    kept.append({"branch": branch, "reason": "not an aq/ task branch"})
                return
            head = heads.get(branch)
            if head is None:
                if not quiet:
                    missing.append(branch)
                return
            if branch in holds:
                kept.append({"branch": branch, "reason": holds[branch]})
            elif head == delivered or await on_main(head):
                delete[branch] = {"head": head, "kind": kind, "reason": landed_by}
            elif not quiet:
                kept.append(
                    {"branch": branch, "reason": f"has commits not on {repo.default_branch}"}
                )

        members = _manifest_members(row["manifest"])
        for member in members:
            task = await self.resolve_task(member["task_id"])
            if task is None:
                kept.append(
                    {"branch": f"aq/{member['task_id']}", "reason": "task is unknown"}
                )
                continue
            branch = branch_of(task.branch_name)
            if branch is None:
                continue
            if task.status != TaskStatus.COMPLETED.value:
                kept.append({"branch": branch, "reason": f"task is {task.status}"})
                continue
            await consider(branch, kind="task", delivered=member.get("source_sha"))
            # A fail-close sibling holds a failed attempt: only its ancestry
            # proves that work is on main.
            await consider(branch + "-wip", kind="task", quiet=True)

        def key(member):
            return member["task_id"], member.get("source_sha")

        landed = {key(member) for member in members}
        done = {
            key(member)
            for other in history
            if other["repository_id"] == repo.id
            and other["target_ref"] == target
            and other["state"] in {"delivered", "adopted"}
            for member in _manifest_members(other["manifest"])
        }
        assemblies = {}
        for other in history:
            branch = branch_of(other["target_ref"])
            if other["repository_id"] == repo.id and branch and branch.startswith(ASSEMBLY_PREFIX):
                assemblies.setdefault(branch, []).append(other)
        for branch, rows in sorted(assemblies.items()):
            carried = {key(m) for other in rows for m in _manifest_members(other["manifest"])}
            if not carried & landed:
                continue
            if any(other["state"] != "delivered" for other in rows):
                kept.append({"branch": branch, "reason": "assembly publication is unsettled"})
            elif not carried <= done:
                kept.append({"branch": branch, "reason": "carries work not delivered yet"})
            else:
                latest = max(rows, key=lambda other: (other["created_at"], other["id"]))
                await consider(branch, kind="assembly", delivered=latest["prepared_sha"])

        evidence = row["evidence"] or {}
        if "resolved_by_main_ancestry" in evidence:
            # The batch parked, a repair was filed, then the sources reached
            # main on their own.  A repair that ended without delivering is
            # moot; one still able to run or deliver is held (or will be
            # collected as a member of its own delivery).
            repair = await self.resolve_task(self._repair_identity(row["manifest"]))
            if repair is not None and repair.status in {
                TaskStatus.FAILED.value, TaskStatus.BLOCKED.value,
            }:
                branch = branch_of(repair.branch_name)
                for candidate in (branch, branch and branch + "-wip"):
                    head = heads.get(candidate) if candidate else None
                    if head is None or candidate in delete:
                        continue
                    if candidate in holds:
                        kept.append({"branch": candidate, "reason": holds[candidate]})
                    else:
                        delete[candidate] = {
                            "head": head, "kind": "moot_repair",
                            "reason": f"repair {repair.task_id} {repair.status}; its sources "
                                      f"reached {repo.default_branch} ({row['id']})",
                        }
        return {"delete": delete, "kept": kept, "missing": missing}

    async def _record_branch_cleanup(self, row, now, *, plan=None, deletion=None, error=None):
        """Write one attempt's result onto *row*; log the refs it deleted."""
        evidence = row["evidence"] or {}
        previous = evidence.get(BRANCH_CLEANUP_KEY) or {}
        attempts = int(previous.get("attempts", 0)) + 1
        deleted = list(previous.get("deleted", []))
        fresh = []
        if error is None:
            kept, failed = list(plan["kept"]), []
            outcomes, bundled = deletion["outcomes"], set(deletion["bundled"])
            for branch, entry in sorted(plan["delete"].items()):
                outcome = outcomes.get(branch)
                if outcome == "deleted":
                    fresh.append({"branch": branch, "sha": entry["head"], "kind": entry["kind"]})
                    if branch in bundled:
                        fresh[-1]["backup"] = deletion["bundle"]
                elif outcome == "moved":
                    kept.append({"branch": branch, "reason": "moved while being deleted"})
                else:
                    failed.append(branch)
            deleted.extend(fresh)
            missing = plan["missing"]
            if failed:
                error = f"{len(failed)} delete(s) not confirmed: {', '.join(failed[:5])}"
        else:
            kept, missing = previous.get("kept", []), previous.get("missing", [])
        if error is None:
            state, next_attempt = "complete", None
        elif attempts >= BRANCH_CLEANUP_MAX_ATTEMPTS:
            state, next_attempt = "exhausted", None
        else:
            state = "pending"
            next_attempt = now + min(
                BRANCH_CLEANUP_RETRY_SECONDS * 2 ** (attempts - 1),
                BRANCH_CLEANUP_RETRY_MAX_SECONDS,
            )
        record = {
            "state": state,
            "attempts": attempts,
            "next_attempt_at": next_attempt,
            "last_error": error,
            "deleted": deleted,
            "kept": kept,
            "missing": missing,
            "log": (deletion or {}).get("log") or previous.get("log"),
            "updated_at": now,
        }
        # Annotation only: it changes nothing readiness reads, so no
        # projection recompute and no ``updated_at`` bump on the row.
        async with self.db._engine.begin() as conn:
            await conn.execute(
                update(deliveries)
                .where(deliveries.c.id == row["id"])
                .values(evidence={**evidence, BRANCH_CLEANUP_KEY: record})
            )
        if fresh:
            await self.db.log_event(
                "development.branches_deleted",
                project_id=row["project_id"],
                payload=json.dumps({"delivery_id": row["id"], "deleted": fresh}),
            )
            logger.info(
                "development delivery %s: deleted %d obsolete branch(es) from origin",
                row["id"], len(fresh),
                extra={"batch": row["id"], "project": row["project_id"]},
            )
        if state == "exhausted" and previous.get("state") != "exhausted":
            logger.warning(
                "development delivery %s: branch cleanup gave up after %d attempts: %s",
                row["id"], attempts, error,
                extra={"batch": row["id"], "project": row["project_id"]},
            )
        return {"delivery_id": row["id"], **record, "deleted_now": fresh}

    async def stale_branches(self, project_id, *, delete=False, now=None):
        """Origin ``aq/`` branches the branch policy lets go of, and what holds the rest.

        The view behind ``aq doctor --check git.stale_branches`` (and the
        supervisor's stall sweep): branches whose work is on the default
        branch, ``aq/integration/*`` refs whose owner is released and whose
        operation finished, and branches of FAILED or abandoned tasks 14 days
        after they went terminal — minus everything
        :func:`live_branch_references` holds.  See
        :func:`src.integration.delivery_branches.find_stale_branches`.  With
        *delete*, each stale branch is backed up, logged and deleted on a
        lease at the head it was found at (:func:`delete_branches`).
        """
        now = time.time() if now is None else now
        project = await self.db.get_project(project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated repository")
        repo = await self.db.get_repo(project.integration_repository_id)
        if repo is None or not repo.url:
            raise ValueError("project repository needs a remote URL")
        async with self.exclusion(repo.id):
            store = await self.store(repo)
            async with self.db._engine.connect() as conn:
                holds = await live_branch_references(conn)
                released = await released_integration_refs(conn)
                expired = await expired_task_branches(conn, now=now)
            report = await find_stale_branches(
                self.run_git, store, default_branch=repo.default_branch, holds=holds,
                released=released, expired=expired,
            )
            report.update(project_id=project_id, repository_id=repo.id)
            if not delete or not report["stale"]:
                return report
            deletion = await delete_branches(
                self.git, self.run_git, store,
                {e["branch"]: {"head": e["head"], "reason": e["reason"]} for e in report["stale"]},
                default_branch=repo.default_branch, main_head=report["main_head"],
                backup_dir=self.backup_dir, repository_id=repo.id, now=now,
            )
        outcomes = deletion["outcomes"]
        deleted = [e for e in report["stale"] if outcomes.get(e["branch"]) == "deleted"]
        report["deleted"] = deleted
        report["moved"] = [b for b, o in sorted(outcomes.items()) if o == "moved"]
        report["failed"] = [b for b, o in sorted(outcomes.items()) if o == "failed"]
        report["backup"] = {
            "bundle": deletion["bundle"], "bundled": deletion["bundled"], "log": deletion["log"],
        }
        if deleted:
            await self.db.log_event(
                "git.stale_branches_deleted",
                project_id=project_id,
                payload=json.dumps({
                    "repository_id": repo.id, "deleted": deleted, **report["backup"],
                }),
            )
        return report

    async def configure(self, project_id, policy, *, reason, operator_id):
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        from src.database.tables import (
            integration_batches,
            integration_branch_owners,
            integration_legacy_suppression,
            integration_promotion_intents,
            project_integration_leases,
        )
        from src.integration.live_operations import describe_live_operation, live_operations_on

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
            current_mode = await conn.scalar(
                select(projects.c.hierarchical_integration_mode).where(projects.c.id == project_id)
            )
            # Development does not run hierarchy repair operations.  Leaving
            # one behind makes it permanently unschedulable, so a transition
            # must make the operator explicitly settle it first.
            if current_mode in {"hierarchy", "train"}:
                live_operations = await live_operations_on(conn, project_id)
                active_batches = (
                    await conn.execute(
                        select(
                            integration_batches.c.id,
                            integration_batches.c.lifecycle,
                            integration_batches.c.cleanup_state,
                        )
                        .where(integration_batches.c.project_id == project_id)
                        .where(
                            integration_batches.c.lifecycle.in_(
                                (
                                    "sealing",
                                    "sealed",
                                    "building",
                                    "testing",
                                    "repairing",
                                    "human_blocked",
                                    "promoting",
                                    "cleanup_pending",
                                )
                            )
                            | (
                                (integration_batches.c.lifecycle == "promoted")
                                & (integration_batches.c.cleanup_state != "complete")
                            )
                        )
                        .order_by(integration_batches.c.created_at, integration_batches.c.id)
                    )
                ).mappings().all()
                lease = (
                    await conn.execute(
                        select(project_integration_leases).where(
                            project_integration_leases.c.project_id == project_id
                        )
                    )
                ).mappings().one_or_none()
                if live_operations or active_batches or lease is not None:
                    details = [
                        describe_live_operation(operation) for operation in live_operations
                    ]
                    details.extend(
                        f"batch {batch['id']} ({batch['lifecycle']}, cleanup "
                        f"{batch['cleanup_state']})"
                        for batch in active_batches
                    )
                    if lease is not None:
                        details.append(
                            f"lease for batch {lease['batch_id']} held by {lease['owner_id']}"
                        )
                    raise DevelopmentBusy(
                        "cannot switch hierarchy integration to development while legacy "
                        "integration work remains: " + "; ".join(details)
                    )
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
        from src.integration.owner_recovery import RECOVERABLE_STATES

        if not reason.strip():
            raise ValueError("cancellation reason is required")
        recovery_owner_ids: list[str] = []
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
            recovery_owner_ids = [
                row["id"]
                for row in current_owners
                if row["handoff_state"] in RECOVERABLE_STATES
            ]
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
                from src.database.queries.task_identity import resolve_task_identity_on

                parent = await resolve_task_identity_on(conn, operation["parent_task_id"])
                if parent is None:
                    raise ValueError("operation parent identity is missing")
                project_id, repository_id = parent.project_id, parent.repo_id
                target_ref = parent.branch_name or ""
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
            # Cancelling obsoletes the operation's delegates: settle them in
            # the same transaction rather than leaving tickets nothing will
            # ever close.  A delegate whose writer is being *preserved* as
            # quarantine still has a live session, so it is skipped here and
            # picked up once that session is gone.
            releases, transitions = await release_delegates_on(
                self.db,
                conn,
                now=now,
                released_by="integration_cancel_preserving",
                operation_ids=[operation_id],
            )
            result = {
                "outcome": "cancelled",
                "operation_id": operation_id,
                "preserved_owners": [r["ref"] for r in retained if r["session_id"]],
                "reason": reason,
                "released_delegates": [row["task_id"] for row in releases],
            }
        for transition in transitions:
            await self.db.log_blocked_flips(transition.flipped)
            await self.db._notify_settled(transition.settled)
            await self.db._notify_ready(transition.ready)
        if self.owner_recovery is not None and recovery_owner_ids:
            await self.owner_recovery.recover_many(
                recovery_owner_ids, principal="cancel_preserving"
            )
        if operation["batch_id"]:
            # The aborted train's request would otherwise hold the project's
            # schedule until someone noticed; preserved writes keep it held.
            from src.integration.stale_schedule import release_ended_batch_request

            schedule_release = await release_ended_batch_request(
                self.db,
                operation["batch_id"],
                now=now,
                released_by="integration_cancel_preserving",
                reason=reason,
            )
            if schedule_release is not None:
                result["release"] = schedule_release
        return result

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

    async def ensure_repair(
        self, project_id, repository_id, manifest, candidate_sha, *, reason, diagnostics=None,
        parked=None,
    ):
        """File one deliberately-rooted repair with provenance and delivery holds.

        Development delivery can park a *set* of source tasks.  It must not
        pick one arbitrary source as a structural parent: that would both hide
        the other origins and make a multi-source repair look like it belongs
        to only one completion.  Repairs are intentionally root tasks, while
        a non-blocking ``discovered-from`` edge records every origin.

        For one original source, the repair is filed as its child even though the
        source is already checkpointed.  The hierarchy writer has a narrowly
        authorised completed-parent exception for this case; it preserves the
        source placement without asking a completed task to execute again.
        A repair-of-repair remains rooted so the bounded recovery chain cannot
        consume structural hierarchy depth.

        For a shared repair, the reverse edge is deliberately different: every parked source
        ``blocks`` on the repair.  In development mode that edge is satisfied
        only after the repair has been delivered, rather than merely closed.
        This makes a shared repair required work for *all* of its sources and
        prevents a delivered repair from falsely releasing just the arbitrary
        source chosen as a parent.  Do not use a parent-child edge here: a
        repair is created after its source has checkpointed/completed, and a
        parent-child edge plus this required reverse edge would be a blocking
        cycle.
        """
        from src.models import DepType, Task, TaskType

        identity = self._repair_identity(manifest)
        # Archive-aware, or an archived repair reads back as "never filed" and
        # is recreated as a fresh READY task on every tick.  That is how
        # ``development-repair-dfda02e25d80e0d1ab2d`` came to sit in ``tasks``
        # as READY while ``archived_tasks`` held the same id COMPLETED.
        if await self.resolve_task(identity) is not None:
            return identity
        single_original_source = len(manifest) == 1 and not str(
            manifest[0]["task_id"]
        ).startswith("development-repair-")
        # Resolve every source once, through the archive as well as the live
        # table, and decide the whole batch before writing anything.
        resolved, missing = {}, []
        for member in manifest:
            source_id = member["task_id"]
            source = await self.resolve_task(source_id)
            if (
                source is None
                or source.project_id != project_id
                or (source.archived and not source.terminal)
            ):
                missing.append(source_id)
            else:
                resolved[source_id] = source
        if missing:
            # A source that exists nowhere is a fact about the batch, not a
            # reason to abort the sweep for every other batch and project.
            self._name_diagnostic(
                diagnostics,
                kind="repair_source_missing",
                task_ids=missing,
                detail=(
                    f"{len(missing)} repair source(s) resolve in neither tasks nor "
                    f"archived_tasks for project '{project_id}': {', '.join(sorted(missing))}"
                ),
            )
            return None
        generation = 1
        for source in resolved.values():
            if source.task_id.startswith("development-repair-"):
                match = re.search(r"Development repair generation: (\d+)", source.description)
                generation = max(generation, (int(match.group(1)) if match else 1) + 1)
        if generation > 3:
            return None  # Keep the candidate parked for operator inspection; no unbounded repair chain.
        sources = "\n".join(f"- {m['task_id']}: {m.get('source_sha')}" for m in manifest)
        repair = Task(
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
                    + self._repair_failure_text(project_id, parked)
                ),
                branch_name="aq/" + identity,
                status=TaskStatus.READY,
                task_type=TaskType.BUGFIX,
                max_retries=3,
            )
        # Service work has no authenticated held-task context, so root
        # placement is an explicit policy choice rather than an accidental
        # omission.  Keep all source provenance independently of placement.
        async with self.db.immediate() as conn:
            await self.db.create_task(repair, conn=conn)
            for member in manifest:
                source_id = member["task_id"]
                source = resolved[source_id]
                if source.archived:
                    # ``task_dependencies.depends_on_task_id`` references
                    # ``tasks.id``, so no edge to an archived source can exist.
                    # The source is terminal, which is to say already
                    # satisfied, and the repair's description still names the
                    # revision.  Record that and carry on; the schema's limit
                    # is not a reason to fail the batch.
                    logger.info(
                        "development repair %s: source %s is archived (%s); "
                        "provenance edge skipped, source already satisfied",
                        identity,
                        source_id,
                        source.status,
                        extra={"repair": identity, "source": source_id, "status": source.status},
                    )
                    continue
                await self.db.add_dependency(
                    identity, source_id, DepType.DISCOVERED_FROM.value,
                    description=reason, conn=conn,
                )
                # A provenance edge answers where the repair came from; it
                # must never be mistaken for a release condition.  The
                # source's blocking edge supplies that condition without
                # giving the repair a reverse dependency on any source.
                if not single_original_source:
                    await self.db.add_dependency(
                        source_id, identity, DepType.BLOCKS.value,
                        description=f"required development repair: {reason}", conn=conn,
                    )
            if single_original_source and not resolved[manifest[0]["task_id"]].archived:
                await self.db.set_parent(
                    identity,
                    manifest[0]["task_id"],
                    conn=conn,
                    description=f"required development repair: {reason}",
                    integration_authorized=True,
                    completed_parent_for_repair=True,
                )
        await self.db.set_task_meta(identity, "development_repair_sources", manifest)
        if parked is not None:
            await self.db.set_task_meta(
                identity, REPAIR_EVIDENCE_KEY, self._repair_evidence(parked)
            )
        return identity

    @staticmethod
    def _repair_evidence(parked):
        """What the parked batch recorded, compact enough for task metadata."""
        evidence = parked.get("evidence") or {}
        return {
            "delivery_id": parked["id"],
            "reason": parked.get("reason"),
            "kind": evidence.get("kind"),
            "conclusion": evidence.get("conclusion"),
            "head_sha": evidence.get("head_sha") or parked.get("prepared_sha"),
            "failing_tests": validation_outcomes.failing_tests(evidence),
            "checks": [
                {
                    key: check.get(key)
                    for key in (
                        "command", "exit_code", "outcome", "detail", "duration_seconds",
                        "slot_wait_seconds", "run_seconds", "summary",
                    )
                    if key in check
                }
                for check in evidence.get("checks") or []
                if isinstance(check, dict)
            ],
            "merge_conflict": (
                evidence.get("detail") if evidence.get("kind") == "merge_conflict" else None
            ),
        }

    @staticmethod
    def _repair_failure_text(project_id, parked):
        """The part of a repair description that names what failed."""
        if parked is None:
            return ""
        evidence = parked.get("evidence") or {}
        lines = [
            "",
            "",
            (
                f"What failed (development delivery row {parked['id']}, "
                f"`aq integration status {project_id}`):"
            ),
        ]
        if evidence.get("kind") == "merge_conflict":
            detail = str(evidence.get("detail") or "").strip()
            lines += ["Merge conflict:", "```", detail[-REPAIR_OUTPUT_TAIL_CHARS:], "```"]
            return "\n".join(lines)
        tests = validation_outcomes.failing_tests(evidence)
        if tests:
            lines.append("Failing tests:")
            for test in tests[:REPAIR_TESTS_LISTED]:
                reason = f" — {test['reason']}" if test["reason"] else ""
                lines.append(f"- {test['id']}{reason}")
            if len(tests) > REPAIR_TESTS_LISTED:
                lines.append(f"- … and {len(tests) - REPAIR_TESTS_LISTED} more (see the row)")
        else:
            lines.append("Failing tests: none identified in the output; see the tail below.")
        for check in evidence.get("checks") or []:
            if not isinstance(check, dict) or check.get("exit_code") == 0:
                continue
            timing = ""
            if "run_seconds" in check:
                timing = (
                    f" after running {check['run_seconds']:.0f}s "
                    f"({check.get('slot_wait_seconds', 0):.0f}s queued for a test slot)"
                )
            lines.append(f"Command `{check.get('command')}` exited {check.get('exit_code')}{timing}.")
            output = str(check.get("output") or "").rstrip()
            if output:
                lines += ["Output tail:", "```", output[-REPAIR_OUTPUT_TAIL_CHARS:], "```"]
        return "\n".join(lines)
