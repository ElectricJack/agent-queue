"""Development delivery: Git manifests, short publication exclusion, and recoverable facts.

No synthetic review/CI receipts are written. Legacy episodes remain audit history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shlex
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, text

from src.database.tables import (
    events,
    task_metadata,
)
from src.git.manager import GitError, GitManager
from src.integration.delivery_branches import (
    ASSEMBLY_PREFIX,
    TASK_BRANCH_PREFIX,
    branch_of,
    completed_branch_tasks,
    delete_branches,
    expired_task_branches,
    find_stale_branches,
    live_branch_references,
    released_integration_refs,
    remote_heads,
    repository_protected_branches,
)
from src.integration.delivery_truth import (
    DeliveryState,
    delivery_snapshot,
    load_delivery_requests,
)
from src.integration.regeneration_settings import REGENERATION_TIMEOUT_SECONDS
from src.integration.publishable_artifact import (
    EMPTY_SOURCE_KEY as EMPTY_SOURCE_KEY,
)
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Statuses :meth:`Database.archive_task` accepts.  Anything in the archive is
#: already in one of them, so an archived source has finished being worked on.
TERMINAL_STATUSES = frozenset(
    {TaskStatus.COMPLETED.value, TaskStatus.FAILED.value, TaskStatus.BLOCKED.value}
)

#: Evidence key on a default-branch journal row recording the removal of the
#: refs that delivery made obsolete (:meth:`DevelopmentPrimitives.
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


def _publishable_object_task(task):
    """Experimental object candidates are artifact-only, including on replay."""
    marker = task_metadata.alias()
    return ~select(marker.c.task_id).where(
        marker.c.task_id == task.c.id,
        marker.c.key == "object_experiment",
    ).correlate(task).exists()
#: The ``merge`` attribute a repository gives its generated artifacts in
#: ``.gitattributes``.  Under a ``regenerate`` policy the publisher defines this
#: driver for its own merges, then rebuilds the files with the policy's command.
#: Legacy reviewed bookkeeping requires verified shard provenance first.
GENERATED_MERGE_DRIVER = "aq-generated"
GENERATED_MERGE_CONFIG = (
    "-c", f"merge.{GENERATED_MERGE_DRIVER}.name=generated artifact, rebuilt after the merge",
    "-c", f"merge.{GENERATED_MERGE_DRIVER}.driver="
    f"{shlex.quote(sys.executable)} "
    f"{shlex.quote(str(Path(__file__).with_name('generated_merge_driver.py')))} %O %A %B %P",
)
REGENERATION_OUTPUT_TAIL_CHARS = 4000


#: Operation states whose action is still in flight and owns its manifest.
#: ``finished`` and ``cancelled`` actions are over; neither answers delivery.
OPEN_OPERATION_STATES = ("prepared", "publishing", "parked")


async def operation_rows_on(conn, project_ids=None, *, states=None):
    """Latest event revisions of real publisher actions, independent of git truth.

    ``None`` *project_ids* reads every project (fleet-wide diagnostics). *states*
    narrows to operations whose latest revision is in one of them.
    """
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    identity = cast(events.c.payload, JSONB)["id"].as_string()
    query = select(events.c.payload).where(events.c.event_type == "development.operation")
    if project_ids is not None:
        query = query.where(events.c.project_id.in_(project_ids))
    payloads = (await conn.execute(
        query.distinct(identity).order_by(identity, events.c.id.desc())
    )).scalars()
    rows = (json.loads(payload) for payload in payloads)
    return sorted((row for row in rows if states is None or row["state"] in states),
                  key=lambda row: (row["created_at"], row["id"]))


async def revise_operation_on(conn, identity, revise):
    """Append a revision of operation *identity* on the caller's transaction.

    Serializes revisions of one operation, including lock-free adoption and
    cleanup, so evidence never overwrites a concurrent revision. *revise* maps
    the latest revision to the values to change, or ``None`` to leave it as is.
    Returns that latest revision, or ``None`` for an unknown operation.
    """
    from sqlalchemy import cast
    from sqlalchemy.dialects.postgresql import JSONB

    await conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:id, 0))"),
                       {"id": "development-operation:" + identity})
    payload = await conn.scalar(select(events.c.payload).where(
        events.c.event_type == "development.operation",
        cast(events.c.payload, JSONB)["id"].as_string() == identity,
    ).order_by(events.c.id.desc()).limit(1))
    if payload is None:
        return None
    current = json.loads(payload)
    values = revise(current)
    if values is not None:
        await conn.execute(DevelopmentPrimitives._operation_insert(
            **{**current, **values, "updated_at": time.time()}
        ))
    return current


def _manifest_members(manifest):
    return [m for m in manifest or [] if isinstance(m, dict) and m.get("task_id")]




def row_repair_identity(row, identity=None):
    """The repair carrying parked *row*.

    A row names it (``evidence.repair_id``) when its manifest's usual repair
    was filed for another target; otherwise it is the manifest's digest.
    """
    recorded = (row.get("evidence") or {}).get("repair_id")
    if isinstance(recorded, str) and recorded.startswith("development-repair-"):
        return recorded
    return (identity or DevelopmentPrimitives._repair_identity)(row["manifest"])


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
    #: Command that rebuilds the repository's generated artifacts -- the paths
    #: ``.gitattributes`` marks ``merge=aq-generated`` -- in the checkout it runs
    #: in, e.g. ``scripts/regenerate-generated.sh``.  Run without a shell after
    #: any merge that both sides changed a generated file in.  Unset, a conflict
    #: in those files parks its source like any other conflict.
    regenerate: str | None = None
    #: Seconds one regeneration may run before the merge counts as failed.
    regenerate_timeout_seconds: int = Field(default=REGENERATION_TIMEOUT_SECONDS, gt=0, le=3600)

    def checked(self):
        if self.validation not in {"focused", "advisory", "none"}:
            raise ValueError("validation must be focused, advisory, or none")
        if self.validation == "focused" and not self.commands:
            raise ValueError("focused validation requires at least one local command")
        if self.regenerate is not None:
            try:
                argv = shlex.split(self.regenerate)
            except ValueError as exc:
                raise ValueError(f"regenerate is not a valid command line: {exc}") from exc
            if not argv:
                raise ValueError("regenerate must name a command")
        return self


class DevelopmentBusy(RuntimeError):
    pass


@asynccontextmanager
async def publisher_exclusion(db, repository_id, subject=None):
    """Serialize Git ports and leased cleanup on the repository publisher fence.

    Root writes also require the exact live root subject. Other subject kinds
    use their own durable subject and branch-fence checks in SubjectGitAuthority.
    Cleanup only receives exclusion, never root mutation authority.
    """
    key = int.from_bytes(hashlib.sha256(repository_id.encode()).digest()[:8], "big", signed=True)
    async with db._engine.execution_options(isolation_level="AUTOCOMMIT").connect() as conn:
        acquired = await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        if not acquired:
            raise DevelopmentBusy("repository publisher is already running")
        try:
            from src.integration.engine import EngineRefused, RootEngineOwnership

            try:
                ownership = RootEngineOwnership(db)
                guard = (
                    ownership.operation(repository_id, subject=subject, publisher=True)
                    if subject is not None and subject.kind.value == "root_batch"
                    else ownership.publisher_exclusion(repository_id)
                )
                async with guard:
                    yield
            except EngineRefused as exc:
                raise DevelopmentBusy(str(exc)) from exc
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


class DevelopmentPrimitives:
    """Retained Git storage, bounded repair filing and branch cleanup.

    The subject reconciler owns scheduling, validation and publication.
    """

    def __init__(self, db, *, data_dir, git: GitManager, confirm_stopped=None,
                 owner_recovery=None, job_client=None):
        self.db = db
        self.data_dir = Path(data_dir) / "development-integration"
        self.backup_dir = Path(data_dir) / "backups" / "branch-deletions"
        self.git = git
        self.confirm_stopped = confirm_stopped
        self.owner_recovery = owner_recovery
        self.job_client = job_client


    def exclusion(self, repository_id):
        return publisher_exclusion(self.db, repository_id)


    @property
    def delivery_observer(self):
        """Git delivery truth for this service's readers, in its own isolated store."""
        from src.integration.delivery_observer import DeliveryObserver

        observer = getattr(self, "_delivery_observer", None)
        if observer is None or observer.git is not self.git:
            observer = DeliveryObserver(self.db, git=self.git, data_dir=self.data_dir.parent)
            self._delivery_observer = observer
        return observer


    async def branch_holds(self, branches):
        """:func:`live_branch_references` with git proof for the tasks owning *branches*.

        Only the completed development tasks whose branch may be deleted are
        observed.  A view whose target moved meanwhile proves nothing, so their
        branches stay held.
        """
        async with self.db._engine.connect() as conn:
            owners = await completed_branch_tasks(conn, branches)
        delivery = await self.delivery_observer.observe(owners) if owners else None
        if delivery is not None and not await delivery.fresh():
            delivery = None
        async with self.db._engine.connect() as conn:
            return await live_branch_references(conn, delivery=delivery)


    async def run_git(self, store, *args):
        result = await self.git.arun_git_result(list(args), cwd=str(store))
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "Git command failed")
        return result.stdout.strip()


    async def resolved_backup(self, repository_id):
        from src.projects.storage_binding import repository_path

        return await repository_path(self.db, repository_id, self.backup_dir)

    def _store_path(self, repo):
        return self.data_dir / hashlib.sha256(repo.id.encode()).hexdigest()[:20] / "repository"


    async def store(self, repo, *, fetch=True):
        from src.integration.repository_recovery import (
            fetch_retained_origin, open_retained_checkout,
        )

        from src.projects.storage_binding import repository_path

        path = await repository_path(self.db, repo.id, self._store_path(repo))
        await open_retained_checkout(self.git, path, repo.url)
        if fetch:
            await fetch_retained_origin(self.git, path, repository_url=repo.url,
                                        project_id=repo.project_id, repository_id=repo.id)
        return path


    async def remote(self, store, ref):
        await self.run_git(store, "check-ref-format", ref)
        from src.git.manager import RemoteRefState

        branch = ref.removeprefix("refs/heads/")
        observed = await self.git.als_remote_ref(str(store), branch)
        if observed.state is RemoteRefState.ERROR:
            raise GitError(observed.error or "remote head observation failed")
        return observed.oid


    @staticmethod
    def _operation_insert(**row):
        """Append genuine operation intent/evidence to the existing event log.

        ``finished`` means this action ended, never that a task is delivered.
        The retained manifest is the input to a push/test/repair attempt. Every
        delivery-sensitive reader must independently inspect current git.
        """
        row = {"expected_sha": None, "prepared_sha": None, **row}
        if row["state"] in {"delivered", "adopted"}:
            row["state"] = "finished"
        return insert(events).values(
            event_type="development.operation", project_id=row["project_id"],
            payload=json.dumps(row), timestamp=time.time(),
        )


    async def save(self, row):
        async with self.db._engine.begin() as conn:
            await conn.execute(self._operation_insert(**row))


    async def change(self, identity, **values):
        async with self.db._engine.begin() as conn:
            if await revise_operation_on(conn, identity, lambda _current: values) is None:
                raise ValueError(f"unknown development operation {identity}")


    async def rows(self, project_id):
        """Latest operation revisions; no delivery receipts are read."""
        async with self.db._engine.connect() as conn:
            return await operation_rows_on(conn, [project_id])

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
                and row["state"] == "finished"
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
                holds = await self.branch_holds(heads)
                async with self.db._engine.connect() as conn:
                    protected = await repository_protected_branches(
                        conn, repo.id, default_branch=repo.default_branch,
                    )
                holds.update({branch: "protected promotion target" for branch in protected})
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
                    backup_dir=await self.resolved_backup(repo.id), repository_id=repo.id, now=now,
                    protected=protected,
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
        requests = await load_delivery_requests(
            self.db, {m["task_id"] for other in history for m in _manifest_members(other["manifest"])},
            repository_id=repo.id, target_ref=target,
        )
        truth = await delivery_snapshot(
            self.git, store, project_id=repo.project_id, repository_id=repo.id,
            repository_url=repo.url, target_ref=target,
        )
        if truth.target_oid != main_head:
            raise DevelopmentBusy("cleanup target changed; retry with a fresh snapshot")
        evaluated = await truth.evaluate_many(requests.values())

        def proven(member):
            proof = evaluated.get(member["task_id"])
            return (proof is not None and proof.state is DeliveryState.CONTAINED
                    and proof.source_oid == member.get("source_sha"))

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
            if not proven(member):
                kept.append({"branch": branch, "reason": "current completion is not proven on target"})
                continue
            await consider(branch, kind="task", delivered=member.get("source_sha"))
            # A fail-close sibling holds a failed attempt: only its ancestry
            # proves that work is on main.
            await consider(branch + "-wip", kind="task", quiet=True)

        def key(member):
            return member["task_id"], member.get("source_sha")

        landed = {key(member) for member in members}
        done = {key(member) for other in history
                for member in _manifest_members(other["manifest"]) if proven(member)}
        assemblies = {}
        for other in history:
            branch = branch_of(other["target_ref"])
            if other["repository_id"] == repo.id and branch and branch.startswith(ASSEMBLY_PREFIX):
                assemblies.setdefault(branch, []).append(other)
        for branch, rows in sorted(assemblies.items()):
            carried = {key(m) for other in rows for m in _manifest_members(other["manifest"])}
            if not carried & landed:
                continue
            if any(other["state"] != "finished" for other in rows):
                kept.append({"branch": branch, "reason": "assembly publication is unsettled"})
            elif not carried <= done:
                kept.append({"branch": branch, "reason": "carries work not delivered yet"})
            else:
                latest = max(rows, key=lambda other: (other["created_at"], other["id"]))
                await consider(branch, kind="assembly", delivered=latest["prepared_sha"])

        evidence = row["evidence"] or {}
        if ("resolved_at_target" in evidence or "resolved_by_main_ancestry" in evidence) and all(
            proven(member) for member in members
        ):
            # The batch parked, a repair was filed, then the sources reached
            # main on their own.  A repair that ended without delivering is
            # moot; one still able to run or deliver is held (or will be
            # collected as a member of its own delivery).
            repair = await self.resolve_task(row_repair_identity(row))
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
        if not await truth.is_fresh():
            raise DevelopmentBusy("cleanup target moved; retry")
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
        await self.change(row["id"], evidence={**evidence, BRANCH_CLEANUP_KEY: record})
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
            holds = await self.branch_holds(await remote_heads(self.run_git, store))
            async with self.db._engine.connect() as conn:
                released = await released_integration_refs(conn)
                expired = await expired_task_branches(conn, now=now)
                protected = await repository_protected_branches(
                    conn, repo.id, default_branch=repo.default_branch,
                )
            report = await find_stale_branches(
                self.run_git, store, default_branch=repo.default_branch, holds=holds,
                released=released, expired=expired,
                protected=protected,
            )
            report.update(project_id=project_id, repository_id=repo.id)
            if not delete or not report["stale"]:
                return report
            deletion = await delete_branches(
                self.git, self.run_git, store,
                {e["branch"]: {"head": e["head"], "reason": e["reason"]} for e in report["stale"]},
                default_branch=repo.default_branch, main_head=report["main_head"],
                backup_dir=await self.resolved_backup(repo.id), repository_id=repo.id, now=now,
                protected=protected,
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
