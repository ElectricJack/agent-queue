"""Development delivery: Git manifests, short publication exclusion, and recoverable facts.

No synthetic review/CI receipts are written. Legacy episodes remain audit history.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shlex
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import insert, select, text

from src.database.tables import (
    events,
    task_metadata,
    tasks,
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
    SETTLEMENT_KEY,
    DeliveryState,
    delivery_snapshot,
    load_delivery_requests,
    settlement_fields,
)
from src.integration.publishable_artifact import (
    EMPTY_SOURCE_KEY as EMPTY_SOURCE_KEY,
)
from src.jobs.pytest_output import failing_tests
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
#: Runs whose full evidence a deferral streak row keeps (the count is kept
#: for every run; older runs' output is dropped).
DEFERRAL_RUNS_KEPT = 5
#: Task metadata key holding what a repair's parked batch recorded.
REPAIR_EVIDENCE_KEY = "development_repair_evidence"
#: Chained repairs of one parked batch before the rest is left to an operator.
REPAIR_GENERATIONS = 3
#: How much of a failure a repair description quotes.
REPAIR_TESTS_LISTED = 20
REPAIR_OUTPUT_TAIL_CHARS = 3000


def _publishable_object_task(task):
    """Experimental object candidates are artifact-only, including on replay."""
    marker = task_metadata.alias()
    return ~select(marker.c.task_id).where(
        marker.c.task_id == task.c.id,
        marker.c.key == "object_experiment",
    ).correlate(task).exists()
#: The ``merge`` attribute a repository gives its generated artifacts in
#: ``.gitattributes``.  Under a ``regenerate`` policy the publisher defines this
#: driver for its own merges as a text merge that keeps our side of every
#: overlapping hunk -- it never conflicts -- and then rebuilds the files with
#: the policy's command, so a conflict confined to them never parks a source.
GENERATED_MERGE_DRIVER = "aq-generated"
GENERATED_MERGE_CONFIG = (
    "-c", f"merge.{GENERATED_MERGE_DRIVER}.name=generated artifact, rebuilt after the merge",
    "-c", f"merge.{GENERATED_MERGE_DRIVER}.driver=git merge-file --quiet --ours %A %O %B",
)
REGENERATION_OUTPUT_TAIL_CHARS = 4000


def armed_for_branch_cleanup(evidence):
    """*evidence* with branch cleanup armed, for a row that just landed on main."""
    return {**evidence, BRANCH_CLEANUP_KEY: {"state": "pending", "attempts": 0}}


#: Operation states whose action is still in flight and owns its manifest.
#: ``finished`` and ``cancelled`` actions are over; neither answers delivery.
OPEN_OPERATION_STATES = ("prepared", "publishing", "parked")

#: A held dependency's skip reason, as its dependent's warning names it.  A
#: dependent repeats any of these reasons, so its stall carries the dependency's
#: own recovery advice; every other reason is an ``undelivered_dependency``.
_DEPENDENCY_SKIP_DETAIL = {
    "dependency_cycle": "dependency cycle with",
    "missing_ref": "missing ref for dependency",
    "missing_provenance": "missing git provenance for dependency",
    "invalid_parent_completion": "invalid parent completion for dependency",
    "parent_provenance_mismatch": "parent provenance mismatch for dependency",
    "parent_adoption_provenance_mismatch": "parent adoption provenance mismatch for dependency",
    "missing_or_ambiguous_source": "unresolvable source for dependency",
    "undelivered_dependency": "undelivered dependency",
}


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


def _carried_sources(repair_id, contracts):
    """Every exact revision *repair_id* carries, through the repairs it replaced.

    A repair's ``development_repair_sources`` contract names the revisions it
    replaces.  When one of those is itself a repair, that repair's contract
    names what *it* replaced, and so on: the newest repair of a chain carries
    the whole chain, not only its predecessor.  Reading one contract made a
    generation-3 repair unable to supersede its generation-1 ancestor, so
    every sweep held the three as a dependency cycle (solid-horizon).
    *contracts* maps repair ids to their contracts.  A task reached with two
    different revisions maps to ``None``: which one is carried is unproven.
    """
    carried, pending, seen = {}, [repair_id], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for member in _manifest_members(contracts.get(current)):
            task_id, source = member["task_id"], member.get("source_sha")
            if carried.setdefault(task_id, source) != source:
                carried[task_id] = None
            if task_id in contracts:
                pending.append(task_id)
    return carried


#: A repair in one of these is still being worked on (or will be).
OPEN_REPAIR_STATUSES = frozenset({
    TaskStatus.DEFINED.value, TaskStatus.READY.value, TaskStatus.ASSIGNED.value,
    TaskStatus.IN_PROGRESS.value, TaskStatus.WAITING_INPUT.value, TaskStatus.PAUSED.value,
})
#: Links followed through repairs of repairs.  The generation budget keeps a
#: real chain at three; a longer one is a loop in the journal.
REPAIR_CHAIN_LIMIT = 8


def row_repair_identity(row, identity=None):
    """The repair carrying parked *row*.

    A row names it (``evidence.repair_id``) when its manifest's usual repair
    was filed for another target; otherwise it is the manifest's digest.
    """
    recorded = (row.get("evidence") or {}).get("repair_id")
    if isinstance(recorded, str) and recorded.startswith("development-repair-"):
        return recorded
    return (identity or DevelopmentPrimitives._repair_identity)(row["manifest"])


def repair_chain(manifest, history, statuses, *, repository_id, target_ref, identity=None,
                 verified=(), row=None):
    """Follow a parked batch through its repairs to the one still carrying it.

    A parked batch's repair is ``development-repair-<digest of its manifest>``.
    When that repair closes and its own publication parks too (the target
    moved again), the batch is carried by the repair of *that* row, and so on.
    That chain is what the source's dependents are waiting on.  Reading only
    the first repair made a live chain look like no repair at all
    (``wise-bridge``: fresh-ember.2, nimble-bridge.8).

    *statuses* maps repair task ids to their status, the live table first and
    then the archive; *identity* names a manifest's repair
    (:meth:`DevelopmentPrimitives._repair_identity` by default).  The result's ``open_repair`` names the repair that
    carries the batch, or is ``None`` when nothing will resolve it by itself:

    * ``open``: a repair in the chain is still being worked on;
    * ``awaiting_publication``: the last repair closed and the publisher has
      neither parked nor delivered it yet;
    * ``delivered``: the last repair reached the target, so the next sweep
      adopts the batch;
    * ``missing``: no repair exists for the last parked row (not yet filed, or
      refused, e.g. past the generation budget);
    * ``finished``: the last repair ended without completing;
    * ``loop``: the journal's chain repeats or passes :data:`REPAIR_CHAIN_LIMIT`.

    Pass the parked *row* itself so a repair it names is followed
    (:func:`row_repair_identity`).
    """
    rows = sorted(
        (row for row in history if row.get("repository_id") == repository_id),
        key=lambda row: (row.get("created_at") or 0, str(row.get("id"))),
        reverse=True,
    )
    parked_in = {}
    delivered = set(verified)
    for journal_row in rows:
        members = _manifest_members(journal_row.get("manifest"))
        if journal_row.get("state") == "parked":
            for member in members:
                parked_in.setdefault(member["task_id"], []).append(journal_row)
    identity = identity or DevelopmentPrimitives._repair_identity
    seen = set()

    def outcome(open_repair, state, chain, detail):
        return {"open_repair": open_repair, "state": state, "chain": chain, "detail": detail}

    def follow(repair, chain):
        status = statuses.get(repair)
        chain = [*chain, {"task_id": repair, "status": status}]
        if repair in seen or len(chain) > REPAIR_CHAIN_LIMIT:
            return outcome(None, "loop", chain, f"repair chain repeats at {repair}")
        seen.add(repair)
        if status is None:
            return outcome(None, "missing", chain, f"no repair {repair} exists")
        if status in OPEN_REPAIR_STATUSES:
            return outcome(repair, "open", chain, f"repair {repair} is {status}")
        if status != TaskStatus.COMPLETED.value:
            return outcome(None, "finished", chain, f"repair {repair} ended {status}")
        if repair in delivered:
            return outcome(repair, "delivered", chain, f"repair {repair} was delivered")
        # Duplicate rows of one manifest share one repair: follow each once.
        successors = list(dict.fromkeys(
            row_repair_identity(parked, identity) for parked in parked_in.get(repair, [])
        ))
        if not successors:
            return outcome(
                repair, "awaiting_publication", chain,
                f"repair {repair} closed and awaits publication",
            )
        results = [follow(successor, chain) for successor in successors]
        return next((result for result in results if result["open_repair"]), results[0])

    return follow(
        row_repair_identity(row, identity) if row is not None else identity(manifest), [],
    )


def describe_repair_chain(result):
    """One line naming each repair in *result*'s chain and its status."""
    return " -> ".join(
        f"{link['task_id']} ({link['status'] or 'not filed'})" for link in result["chain"]
    )


async def repair_statuses(conn, project_ids):
    """Every development repair's status in *project_ids*, live table over archive."""
    from src.database.tables import archived_tasks

    statuses = {}
    for table in (archived_tasks, tasks):
        statuses.update((await conn.execute(
            select(table.c.id, table.c.status).where(
                table.c.id.like("development-repair-%"),
                table.c.project_id.in_(list(project_ids)),
            )
        )).all())
    return statuses


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


@dataclass(frozen=True)
class MergeOutcome:
    """What merging one source into the candidate produced.

    ``conflicting_files`` names the conflicts a person has to resolve; generated
    files are never among them under a ``regenerate`` policy.  ``regenerated``
    names the generated files rebuilt after the merge, and ``regeneration`` is
    the evidence of a rebuild that failed.
    """

    ok: bool
    detail: str = ""
    conflicting_files: tuple[str, ...] = ()
    regenerated: tuple[str, ...] = ()
    regeneration: dict | None = None


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
    regenerate_timeout_seconds: int = Field(default=600, gt=0, le=3600)

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
    async with db._engine.connect() as conn:
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


    def _store_path(self, repo):
        return self.data_dir / hashlib.sha256(repo.id.encode()).hexdigest()[:20] / "repository"


    async def store(self, repo, *, fetch=True):
        path = self._store_path(repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not (path / ".git").exists():
            await self.git.acreate_checkout(repo.url, str(path), no_checkout=True)
        if await self.run_git(path, "remote", "get-url", "origin") != repo.url:
            raise ValueError("retained repository URL differs from configured repository")
        if fetch:
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


    async def _reclaim_repair(self, repair_id, target):
        """Drop *repair_id*'s settlement on *target*: a live park needs it again."""
        from sqlalchemy import delete

        async with self.db._engine.begin() as conn:
            value = await conn.scalar(select(task_metadata.c.value).where(
                task_metadata.c.task_id == repair_id, task_metadata.c.key == SETTLEMENT_KEY,
            ))
            if value is None or settlement_fields(value).get("settled_target_ref") != target:
                return
            await conn.execute(delete(task_metadata).where(
                task_metadata.c.task_id == repair_id, task_metadata.c.key == SETTLEMENT_KEY,
            ))
        logger.warning(
            "development publisher: repair %s is needed again on %s; its settlement is withdrawn",
            repair_id, target,
        )


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
                    backup_dir=self.backup_dir, repository_id=repo.id, now=now,
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
                backup_dir=self.backup_dir, repository_id=repo.id, now=now,
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


    async def ensure_repair(
        self, project_id, repository_id, manifest, candidate_sha, *, reason, diagnostics=None,
        parked=None,
    ):
        """File or reuse one bounded repair with provenance and delivery holds.

        Development delivery can park a *set* of source tasks.  It must not
        pick one arbitrary source as a structural parent: that would both hide
        the other origins and make a multi-source repair look like it belongs
        to only one completion.  Repairs are intentionally root tasks, while
        a non-blocking ``discovered-from`` edge records every origin.

        For one original source, the repair is filed as its child even though the
        source is already checkpointed.  The hierarchy writer has a narrowly
        authorised completed-parent exception for this case; it preserves the
        source placement without asking a completed task to execute again.
        At the structural depth cap it is deliberately rooted instead, with
        the same source delivery hold as a shared repair.
        A repair-of-repair remains rooted so the bounded recovery chain cannot
        consume structural hierarchy depth, and no repair ever ``blocks`` on
        another repair: the repaired repair's parked row is what waits for
        this one, and a blocking edge back onto a repair whose contract names
        it would be a dependency cycle.

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
        from src.task_names import MAX_STRUCTURAL_DEPTH

        identity = (
            row_repair_identity(parked) if parked is not None else self._repair_identity(manifest)
        )
        # Archive-aware, or an archived repair reads back as "never filed" and
        # is recreated as a fresh READY task on every tick.  That is how
        # ``development-repair-dfda02e25d80e0d1ab2d`` came to sit in ``tasks``
        # as READY while ``archived_tasks`` held the same id COMPLETED.
        if await self.resolve_task(identity) is not None:
            if parked is not None:
                # A live park on this target owes its repair again, even one
                # an earlier settlement of the same manifest released.
                await self._reclaim_repair(identity, parked["target_ref"])
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
        if generation > REPAIR_GENERATIONS:
            # Keep the candidate parked for operator inspection; no unbounded
            # repair chain.  Say so: a silent refusal leaves the sources
            # parked with no open repair and nothing that names why.
            self._name_diagnostic(
                diagnostics,
                kind="repair_generation_exhausted",
                task_ids=[member["task_id"] for member in manifest],
                detail=(
                    f"{REPAIR_GENERATIONS} repair generations did not deliver "
                    f"{', '.join(sorted(m['task_id'] for m in manifest))}; no further "
                    "repair is filed and the batch stays parked for an operator"
                ),
            )
            return None
        sources = "\n".join(f"- {m['task_id']}: {m.get('source_sha')}" for m in manifest)
        repo = await self.db.get_repo(repository_id)
        target_branch = repo.default_branch
        branches = "\n".join(
            f"- {member['task_id']}: {resolved[member['task_id']].branch_name}"
            for member in manifest
        )
        conflict = parked is not None and parked.get("evidence", {}).get("kind") == "merge_conflict"
        recovery = (
            f"Fetch origin and, in your own task branch (it starts from origin/{target_branch}), "
            "merge each listed source revision by its exact SHA (`git merge <sha>`). Resolve "
            "the named conflicting files and preserve the intended source changes; where a "
            "conflicting file is generated, regenerate it from the merged sources instead of "
            "hand-merging it. Do not rebase, squash or cherry-pick: every listed source "
            "revision must stay an ancestor of your branch, so delivering the repair delivers "
            f"the source too. If origin/{target_branch} moves before you push, merge it again. "
            if conflict else
            f"Preserve their intended changes and resolve against current origin/{target_branch}. "
        ) + self._regeneration_text(parked)
        repair = Task(
                id=identity,
                project_id=project_id,
                repo_id=repository_id,
                title="Repair development integration: " + reason,
                description=(
                    f"Development repair generation: {generation}\nThe development batch parked these source revisions:\n{sources}\n"
                    f"Candidate/base: {candidate_sha}.\nSource branches:\n{branches}\n"
                    f"Publication target: refs/heads/{target_branch}.\n"
                    + recovery
                    + "Publish the repair on your own task branch. Ordinary merge commits are allowed. "
                    "Run focused local checks and close with actual evidence; no parent verifier or PR is required. "
                    "A passing close attests that every listed source revision is resolved; once your repair "
                    f"is delivered to {target_branch}, that delivery also satisfies those sources for their successors. "
                    f"Do not push {target_branch}. The development publisher will collect your branch. "
                    "This is one resumable repair task; queue and provider waits do not expire it."
                    + self._repair_failure_text(project_id, parked)
                ),
                branch_name="aq/" + identity,
                status=TaskStatus.READY,
                task_type=TaskType.BUGFIX,
                max_retries=3,
                # The routing policy names this origin
                # (``origins.development_repair``) instead of matching the
                # title (mandatory-routing spec §5.3).
                created_by_kind="development_repair",
            )
        # Service work has no authenticated held-task context, so root
        # placement is an explicit policy choice rather than an accidental
        # omission.  Keep all source provenance independently of placement.
        async with self.db.immediate() as conn:
            nest_repair = False
            if single_original_source and not resolved[manifest[0]["task_id"]].archived:
                # The source may already be at the hierarchy depth cap. A
                # failed set_parent would roll back the repair every tick.
                # Hold the hierarchy lock through the placement write so a
                # concurrent move cannot invalidate this decision.
                await self.db.lock_hierarchy_project(conn, project_id)
                nest_repair = await self.db.structural_depth(
                    manifest[0]["task_id"], conn=conn
                ) < MAX_STRUCTURAL_DEPTH
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
                # A source that is itself a repair gets no such edge: its
                # parked row already waits for this repair's delivery, and
                # an edge onto a repair whose contract names it is a cycle.
                # Three generations of that held every repair of the chain
                # forever (solid-horizon).
                if not nest_repair and not source_id.startswith("development-repair-"):
                    await self.db.add_dependency(
                        source_id, identity, DepType.BLOCKS.value,
                        description=f"required development repair: {reason}", conn=conn,
                    )
            if nest_repair:
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
            # The target this repair publishes to; after a retarget it is owed
            # nowhere else (development_settlement.repair_target_of).
            "target_ref": parked.get("target_ref"),
            "reason": parked.get("reason"),
            "kind": evidence.get("kind"),
            "conclusion": evidence.get("conclusion"),
            "head_sha": evidence.get("head_sha") or parked.get("prepared_sha"),
            "failing_tests": failing_tests(evidence),
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
            "conflicting_files": evidence.get("conflicting_files", []),
            "conflict_with": evidence.get("conflict_with"),
            "conflicting_members": evidence.get("conflicting_members", []),
            "regeneration": evidence.get("regeneration"),
        }


    @staticmethod
    def _conflict_attribution_text(evidence):
        """What a parked conflict was proven to conflict with; rows before this say nothing."""
        kind = evidence.get("conflict_with")
        target = f"{evidence.get('target_ref')} at {evidence.get('target_sha')}"
        if kind == "target":
            return [f"Conflicts with the target itself: {target}."]
        if kind == "members":
            return [
                f"Conflicts with work merged before it in the same batch (on {target}):",
                *(f"- {member['task_id']} ({member['source_sha']}): "
                  + ", ".join(member.get("paths") or [])
                  for member in evidence.get("conflicting_members") or []),
            ]
        if kind == "batch":
            return [(
                f"Merges cleanly onto {target}, but conflicts with the batch aggregate "
                f"{evidence.get('aggregate_sha')}; no single member was proven responsible."
            )]
        return []


    @staticmethod
    def _regeneration_text(parked):
        """How a repair treats generated files, when the batch could rebuild them."""
        command = ((parked or {}).get("evidence") or {}).get("regenerate")
        if not command:
            return ""
        return (
            f"Files marked merge={GENERATED_MERGE_DRIVER} in .gitattributes are generated: never "
            "hand-merge them. Take either side (`git checkout --ours -- <path>`), resolve the "
            f"source files, then run `{command}` and commit what it writes. "
        )


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
            files = evidence.get("conflicting_files") or []
            regeneration = evidence.get("regeneration")
            if files:
                lines += ["Conflicting files:", *(f"- {path}" for path in files)]
            lines += DevelopmentPrimitives._conflict_attribution_text(evidence)
            if regeneration:
                # The merge itself succeeded; rebuilding its generated files did not.
                lines += [
                    (
                        f"Regenerating the generated files with "
                        f"`{regeneration.get('command')}` failed: {regeneration.get('detail')}."
                    ),
                    "Regeneration output:", "```", detail[-REPAIR_OUTPUT_TAIL_CHARS:], "```",
                ]
            else:
                lines += ["Merge conflict:", "```", detail[-REPAIR_OUTPUT_TAIL_CHARS:], "```"]
            return "\n".join(lines)
        tests = failing_tests(evidence)
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
