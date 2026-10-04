"""Development delivery: Git manifests, short publication exclusion, and recoverable facts.

No synthetic review/CI receipts are written. Legacy episodes remain audit history.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, insert, select, text, update

from src.database.queries.blocked_state import (
    blocked_predicate,
    obsolete_marker,
)
from src.database.tables import (
    events,
    projects,
    repos,
    sessions,
    task_completion_records,
    task_metadata,
    tasks,
)
from src.git.manager import GitError, GitManager, commit_identity, is_valid_git_oid
from src.integration import development_validation as validation_outcomes
from src.integration.delegate_release import release_delegates_on
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
)
from src.integration.delivery_truth import (
    SETTLEMENT_KEY,
    DeliverySnapshot,
    DeliveryState,
    delivery_snapshot,
    load_delivery_requests,
    settlement_fields,
)
from src.integration.development_settlement import (
    DELIVERED_TO_PREVIOUS_TARGET,
    OPERATOR_SETTLED,
    REPAIR_FOR_PREVIOUS_TARGET,
    SOURCES_NOT_OWED,
    notify_settlements_on,
    previous_target,
    repair_target_of,
    retarget_of,
    settlement_record,
    write_settlements_on,
)
from src.integration.development_stalls import (
    DEFAULT_STALL_AFTER,
    PUBLISHER_SKIP_KEY,
    PublisherStalls,
    SweepObservation,
    delivery_skip_reason,
)
from src.integration.development_validation import run_check as run_validation_check
from src.integration.parent_engine import parent_engine_guard
from src.integration.provenance import CompletedSource, CompletionIdentity, GitProvenance
from src.integration.publishable_artifact import (
    EMPTY_SOURCE_KEY as EMPTY_SOURCE_KEY,
)
from src.integration.publishable_artifact import (
    has_publishable_artifact,
)
from src.jobs.policy import JobError, presets, validate_args
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
        await conn.execute(DevelopmentIntegration._operation_insert(
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
    return (identity or DevelopmentIntegration._repair_identity)(row["manifest"])


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
    (:meth:`DevelopmentIntegration._repair_identity` by default).  The result's ``open_repair`` names the repair that
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
    identity = identity or DevelopmentIntegration._repair_identity
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
    """Hold *repository_id*'s development publisher lock, or raise ``DevelopmentBusy``.

    A dedicated connection owns a session advisory lock across short DB
    commits.  A process death releases it; durable publishing rows retain
    ambiguous writes.  Anything that rewrites a batch row outside the
    publisher (an obsolete close dropping a parked batch) takes it too.

    *subject* names the root subject whose own mutation this exclusion
    authorizes.  A repository the reconciler engine owns refuses an unnamed
    root mutation outright ("repository root publisher belongs to the
    reconciler"), so the shared primitives have to present the exact subject
    they act for; the ownership check then admits it only while that subject is
    still a live root at the pinned engine version.  Omitting it keeps the
    legacy publisher's meaning unchanged.
    """
    key = int.from_bytes(hashlib.sha256(repository_id.encode()).digest()[:8], "big", signed=True)
    async with db._engine.connect() as conn:
        acquired = await conn.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": key})
        if not acquired:
            raise DevelopmentBusy("repository publisher is already running")
        try:
            from src.integration.engine import EngineRefused, RootEngineOwnership

            try:
                async with RootEngineOwnership(db).operation(
                    repository_id, subject=subject, publisher=True
                ):
                    yield
            except EngineRefused as exc:
                raise DevelopmentBusy(str(exc)) from exc
        finally:
            await conn.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})


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
        stall_after: int = DEFAULT_STALL_AFTER,
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
        #: Skip observations and their bounded stall (``integration.publisher_stall_after``).
        self.stalls = PublisherStalls(
            db, stall_after=stall_after, repair_identity=self._repair_identity
        )

    async def on_task_completed(self, event):
        """Wake delivery without doing Git or validation in the completion path.

        The next integration cycle coalesces completions into one batch. Removing
        the deadline also preserves a wake arriving during an in-flight sweep:
        tick sets the next deadline before awaiting the publisher, not after it.
        Periodic sweeps remain the recovery path for missed events and restarts.
        """
        self.next_due.pop(event.get("project_id"), None)

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

    async def merge_member(self, store, commit, policy) -> MergeOutcome:
        """Merge *commit* into the checkout's detached HEAD.

        Without a ``regenerate`` policy this is a plain ``git merge``.  With one,
        files marked ``merge=aq-generated`` merge through a driver that never
        conflicts, and when both sides changed one of them (or a modify/delete
        left one unmerged) the policy's command rebuilds them from the merged
        sources and the result is folded into the merge commit.  A conflict in
        any other file still fails the merge, as does a rebuild that fails or
        writes a file that is not generated.  A failed merge leaves HEAD and the
        tree as they were.
        """
        before = await self.run_git(store, "rev-parse", "HEAD")
        identity = self.git.resolve_commit_identity().config_args()
        driver = list(GENERATED_MERGE_CONFIG) if policy.regenerate else []
        result = await self.git.arun_git_result(
            [*identity, *driver, "merge", "--no-edit", commit], cwd=str(store)
        )
        unmerged = [] if result.returncode == 0 else (await self.run_git(
            store, "diff", "--name-only", "--diff-filter=U"
        )).splitlines()
        if not policy.regenerate:
            if result.returncode:
                await self.run_git(store, "merge", "--abort")
                return MergeOutcome(False, result.stdout, tuple(unmerged))
            return MergeOutcome(True)
        # A generated file needs rebuilding when both sides changed it: the
        # driver kept our side of whatever overlapped.  Against every merge base,
        # so a criss-cross history cannot hide one.
        both = set(unmerged)
        bases = (await self.git.arun_git_result(
            ["merge-base", "--all", before, commit], cwd=str(store)
        )).stdout.split()
        for base in bases:
            ours = set(await self._changed(store, base, before))
            both |= ours & set(await self._changed(store, base, commit))
        generated = await self.generated_paths(store, both)
        if result.returncode and (not unmerged or not set(unmerged) <= generated):
            await self.run_git(store, "merge", "--abort")
            return MergeOutcome(
                False, result.stdout, tuple(p for p in unmerged if p not in generated)
            )
        if not generated:
            return MergeOutcome(True)
        run = await self.regenerate(store, policy)
        failure = None if run["ok"] else run["detail"]
        written = set()
        if failure is None:
            written = set((await self.run_git(store, "diff", "--name-only", "-z")).split("\0"))
            written |= set((await self.run_git(
                store, "ls-files", "--others", "--exclude-standard", "-z"
            )).split("\0"))
            written.discard("")
            stray = sorted(written - await self.generated_paths(store, written))
            if stray:
                failure = (
                    "the regeneration changed files that are not generated: "
                    + ", ".join(stray[:20]) + (" …" if len(stray) > 20 else "")
                )
        if failure is None:
            staged = sorted(written | set(unmerged))
            if staged:
                await self.run_git_input(
                    store, "\0".join(staged) + "\0", "--literal-pathspecs", "add", "-A",
                    "--pathspec-from-file=-", "--pathspec-file-nul",
                )
            if await self.run_git(store, "diff", "--name-only", "--diff-filter=U"):
                failure = "generated files are still unmerged after the regeneration"
        if failure is None:
            if result.returncode:
                await self.run_git(store, *identity, "commit", "--no-edit", "--no-verify")
            elif (await self.git.arun_git_result(
                ["diff", "--cached", "--quiet"], cwd=str(store)
            )).returncode:
                await self.run_git(
                    store, *identity, "commit", "--amend", "--no-edit", "--no-verify"
                )
            regenerated = tuple(sorted(generated | written))
            logger.info(
                "development publisher: rebuilt %d generated file(s) merging %s",
                len(regenerated), commit,
                extra={"source_sha": commit, "regenerated": list(regenerated)},
            )
            return MergeOutcome(True, regenerated=regenerated)
        # ``reset --hard`` also ends an unfinished merge; the clean removes what
        # a failed rebuild created.
        await self.run_git(store, "reset", "--hard", "-q", before)
        await self.run_git(store, "clean", "-fdq")
        return MergeOutcome(
            False,
            f"{failure}\n{run['output']}".strip(),
            regeneration={**run, "ok": False, "detail": failure},
        )

    async def _changed(self, store, base, commit):
        return (await self.run_git(
            store, "diff", "--name-only", "--no-renames", "-z", base, commit
        )).split("\0")

    async def generated_paths(self, store, paths):
        """The subset of *paths* the checkout's ``.gitattributes`` marks generated."""
        paths = sorted(p for p in paths if p)
        if not paths:
            return set()
        output = await self.run_git_input(
            store, "\0".join(paths) + "\0", "check-attr", "-z", "--stdin", "merge"
        )
        fields = output.split("\0")
        return {
            fields[i] for i in range(0, len(fields) - 2, 3)
            if fields[i + 2] == GENERATED_MERGE_DRIVER
        }

    async def run_git_input(self, store, stdin, *args):
        result = await self.git.arun_git_result(list(args), cwd=str(store), stdin=stdin)
        if result.returncode:
            raise GitError(result.stderr or result.stdout or "Git command failed")
        return result.stdout

    async def regenerate(self, store, policy):
        """Run the policy's regeneration command in *store*, without a shell.

        The command sees a minimal environment: this interpreter's ``bin``
        first on ``PATH`` (the generators need the daemon's packages), and the
        same database refusal sentinels a worker session carries.
        """
        from src.sessions.env import SCRATCH_DB_SENTINEL

        command = policy.regenerate
        env = {
            "PATH": f"{Path(sys.executable).parent}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(Path.home()),
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "AQ_DB_SCOPE": "worker",
            "AQ_DATABASE_URL": SCRATCH_DB_SENTINEL,
            "AGENT_QUEUE_DB": SCRATCH_DB_SENTINEL,
        }
        started = time.monotonic()
        evidence = {"command": command, "exit_code": None, "output": ""}
        try:
            process = await asyncio.create_subprocess_exec(
                *shlex.split(command), cwd=str(store), env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            return {**evidence, "ok": False, "detail": f"could not start `{command}`: {exc}",
                    "duration_seconds": 0.0}
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), policy.regenerate_timeout_seconds
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            await process.wait()
            if isinstance(exc, asyncio.CancelledError):
                raise
            return {**evidence, "ok": False,
                    "detail": f"`{command}` timed out after {policy.regenerate_timeout_seconds}s",
                    "duration_seconds": round(time.monotonic() - started, 3)}
        text = output.decode("utf-8", "replace")[-REGENERATION_OUTPUT_TAIL_CHARS:]
        ok = process.returncode == 0
        return {
            **evidence, "ok": ok, "exit_code": process.returncode, "output": text,
            "detail": "regenerated" if ok else f"`{command}` exited {process.returncode}",
            "duration_seconds": round(time.monotonic() - started, 3),
        }

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

    @asynccontextmanager
    async def read_snapshot(self, repo, target_ref):
        """Yield a freshly fetched snapshot taken in a private repository.

        Reads that only confirm ancestry must neither wait for a sweep holding
        the publisher lock nor move the refs and checkout it is using. The
        private repository borrows the retained store's objects read-only (Git
        never writes into an alternate) and starts from its origin tips, so the
        fetch transfers only what is new. It is removed afterwards.
        """
        shared = self._store_path(repo)
        shared.parent.mkdir(parents=True, exist_ok=True)
        root = Path(tempfile.mkdtemp(prefix="reads-", dir=shared.parent))
        try:
            await self.run_git(root, "init", "--quiet")
            objects = shared / ".git" / "objects"
            if objects.is_dir():
                (root / ".git" / "objects" / "info" / "alternates").write_text(f"{objects}\n")
                try:
                    tips = await self.run_git(
                        shared, "for-each-ref", "--format=update %(refname) %(objectname)",
                        "refs/remotes/origin/",
                    )
                    tips = "".join(
                        line + "\n" for line in tips.splitlines()
                        if not line.startswith("update refs/remotes/origin/HEAD ")
                    )
                    if tips:
                        await self.git.arun_git_result(
                            ["update-ref", "--stdin"], cwd=str(root), stdin=tips
                        )
                except GitError:
                    pass  # Seeding only saves transfer; the fetch below is the truth.
            await self.run_git(root, "remote", "add", "origin", repo.url)
            yield await delivery_snapshot(
                self.git, root, project_id=repo.project_id, repository_id=repo.id,
                repository_url=repo.url, target_ref=target_ref,
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)

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

    async def _has_pending_work(self, project_id, repo, *, now):
        """Check durable work before opening the authenticated Git transport.

        Receipt state cannot exclude completed work: external edits can move
        the target at any time. Only projects without artifact identities can
        skip git. Branch cleanup is included when its retry deadline has passed.
        """
        target = "refs/heads/" + repo.default_branch
        history = await self.rows(project_id)
        if any(row["repository_id"] == repo.id and (
            row["state"] in {"prepared", "publishing", "parked"} or (
                row["target_ref"] == target and
                (row["evidence"].get(BRANCH_CLEANUP_KEY) or {}).get("state") == "pending" and
                float((row["evidence"].get(BRANCH_CLEANUP_KEY) or {}).get("next_attempt_at") or 0) <= now
            )
        ) for row in history):
            return True
        async with self.db._engine.connect() as conn:
            candidate = await conn.scalar(
                select(tasks.c.id).where(
                    tasks.c.project_id == project_id,
                    tasks.c.status == TaskStatus.COMPLETED.value,
                    (tasks.c.repo_id == repo.id) | tasks.c.repo_id.is_(None),
                    has_publishable_artifact(tasks.c.branch_name) | select(
                        task_completion_records.c.id
                    ).where(
                        task_completion_records.c.task_id == tasks.c.id,
                    ).exists(),
                    ~obsolete_marker(tasks),
                    _publishable_object_task(tasks),
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
        pending = [row for row in await self.rows(repo.project_id)
                   if row["repository_id"] == repo.id and row["state"] in {"prepared", "publishing"}]
        if not pending:
            return
        await self.git.afetch_origin(str(store), repository_url=repo.url, all_heads=True)
        for row in pending:
            actual = await self.remote(store, row["target_ref"])
            prepared = row["prepared_sha"]
            if prepared and actual == prepared or (
                actual and prepared and await self.git.ais_ancestor(str(store), prepared, actual)
            ):
                evidence = row["evidence"] | {"reconciled_remote": actual}
                if row["target_ref"] == "refs/heads/" + repo.default_branch and row["manifest"]:
                    evidence = armed_for_branch_cleanup(evidence)
                await self.change(row["id"], state="finished", evidence=evidence)
            elif actual == row["expected_sha"]:
                # Caller holds publication exclusion; no daemon publisher survived.
                await self.change(
                    row["id"],
                    state="cancelled",
                    evidence=row["evidence"] | {"reconciled": "not_applied"},
                )
            elif actual and prepared and await self.git.ais_ancestor(
                str(store), prepared, actual, strict=True
            ) is False:
                # The target moved on without this write: it is not delivered,
                # and nothing failed. The next assembly starts from current git.
                await self.change(row["id"], state="cancelled", evidence=row["evidence"] | {
                    "reconciled": "target_moved", "observed_sha": actual,
                })
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
                snapshot_group=f"{operation_id}:{attempt}" if operation_id else None,
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
            if await self.resolve_task(row_repair_identity(row)) is not None:
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
        if target_ref == "refs/heads/" + repo.default_branch:
            from src.integration.development_adapter import development_subject_owns_target

            if await development_subject_owns_target(
                self.db, repo.project_id, repo.id, target_ref,
            ):
                return {"outcome": "subject_owned", "parked": []}
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
            # Nothing was written and nothing failed: the caller re-assembles
            # from a fresh fetch. A parked row would file a repair for it.
            await self.change(row["id"], state="cancelled", evidence=evidence | {
                "reason": "base_moved", "observed_sha": actual,
            })
            return {"outcome": "base_moved", "id": row["id"], "observed_sha": actual}
        if expected and not await self.git.ais_ancestor(str(store), expected, head):
            raise ValueError("development publication must preserve target history")
        await self.change(row["id"], state="publishing")
        try:
            # Lease binds the actual remote old SHA; zero means ref must not exist.
            await self.git.apush_validated_ref(
                str(store), head, target_ref.removeprefix("refs/heads/"),
                expected_old_oid=expected or "0" * 40,
            )
        except GitError as exc:
            # A refused lease or an uncertain transfer is settled from the
            # remote itself: only our head, or history built on it, applied.
            actual = await self.remote(store, target_ref)
            applied = actual == head or (
                actual is not None
                and await self.git.ais_ancestor(str(store), head, actual, strict=True)
            )
            if applied is None:
                # The moved target is usually not local yet: fetch, never guess.
                await self.git.afetch_origin(str(store), repository_url=repo.url, all_heads=True)
                applied = await self.git.ais_ancestor(str(store), head, actual, strict=True)
            if applied is None:
                raise  # Unknown locally: the next sweep reconciles after its fetch.
            if not applied:
                await self.change(row["id"], state="cancelled", evidence=evidence | {
                    "reason": "base_moved" if actual != expected else "not_applied",
                    "observed_sha": actual, "error": str(exc)[-1000:],
                })
                if actual != expected:
                    return {"outcome": "base_moved", "id": row["id"], "observed_sha": actual}
                raise
        confirmed = await self.remote(store, target_ref)
        if confirmed != head:
            await self.git.afetch_origin(str(store), repository_url=repo.url, all_heads=True)
            if not confirmed or await self.git.ais_ancestor(
                str(store), head, confirmed, strict=True
            ) is not True:
                raise DevelopmentBusy("published ref could not be confirmed; operation retained")
        if arm_branch_cleanup:
            await self.change(
                row["id"], state="finished", evidence=armed_for_branch_cleanup(evidence)
            )
        else:
            await self.change(row["id"], state="finished")
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
        settle_delivered_children=False,
        dry_run=False,
    ):
        """Record a fenced operator completion/equivalence decision in git.

        Ancestry is observed in a private read snapshot, so an ancestry-only
        adoption succeeds while a sweep holds the publisher lock. Accepting a
        source that is not an ancestor is a separate, reasoned decision: it
        takes the publisher exclusion and observes again under it. Either way
        the write refuses a task whose generation or writer changed after it
        was observed.
        """
        if not reason.strip() or not is_valid_git_oid(head_sha) or not task_ids:
            raise ValueError("task ids, exact target SHA and reason are required")
        if settle_delivered_children:
            from src.integration.delivered_parent_adoption import DeliveredParentAdoption

            if len(set(task_ids)) != 1:
                raise ValueError("delivered-child adoption requires exactly one parent")
            return await DeliveredParentAdoption(self).run(
                task_ids[0], project_id=project_id, target_ref=target_ref, head_sha=head_sha,
                reason=reason, operator_id=operator_id, dry_run=dry_run,
                accept_equivalent=accept_equivalent,
            )
        if dry_run:
            raise ValueError("dry-run requires settle_delivered_children")
        project = await self.db.get_project(project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no designated repository")
        repo = await self.db.get_repo(project.integration_repository_id)
        task_ids = sorted(set(task_ids))
        async with self.db._engine.connect() as conn:
            rows = {
                row["id"]: row for row in (
                    await conn.execute(select(tasks).where(tasks.c.id.in_(task_ids)))
                ).mappings()
            }
            generations = await self._completion_generations(conn, task_ids)
            requests = await load_delivery_requests(
                self.db, task_ids, repository_id=repo.id, target_ref=target_ref, conn=conn,
            )
        for task_id in task_ids:
            row = rows.get(task_id)
            if row is None or row["project_id"] != project_id or row["repo_id"] not in {
                None, repo.id,
            }:
                raise ValueError(f"task {task_id} does not belong to the repository project")
            request = requests[task_id]
            if (request.requires_parent_completion and request.parent_completion is None
                and request.parent_adoption is None):
                raise ValueError(f"{task_id}: current verified parent completion is required")
            if row["status"] == "COMPLETED" and (
                not request.completion_id or request.completed_at is None
            ):
                raise ValueError(f"{task_id}: completion generation requires provenance migration")
        identities = {
            task_id: self._adoption_fence(rows[task_id], generations, requests)
            for task_id in task_ids
        }
        manifest = await self._observe_adoption(repo, target_ref, head_sha, identities)
        equivalent = [m["task_id"] for m in manifest if m["acceptance"] != "ancestry"]
        if not equivalent:
            return await self._record_adoption(
                repo, project_id, target_ref, head_sha, manifest, identities,
                reason=reason, operator_id=operator_id,
            )
        if not accept_equivalent:
            raise ValueError(
                f"{equivalent[0]}: source is not an ancestor; explicit equivalence acceptance required"
            )
        async with self.exclusion(repo.id):
            manifest = await self._observe_adoption(repo, target_ref, head_sha, identities)
            return await self._record_adoption(
                repo, project_id, target_ref, head_sha, manifest, identities,
                reason=reason, operator_id=operator_id,
            )

    @staticmethod
    async def _completion_generations(conn, task_ids):
        """Each task's completion count and latest close, the generation adopt fences."""
        return {
            task_id: (count, latest) for task_id, count, latest in await conn.execute(
                select(
                    task_completion_records.c.task_id, func.count(),
                    func.max(task_completion_records.c.completed_at),
                )
                .where(task_completion_records.c.task_id.in_(task_ids))
                .group_by(task_completion_records.c.task_id)
            )
        }

    @staticmethod
    def _adoption_fence(row, generations, requests):
        return (row["status"], row["branch_name"], row["claim_epoch"], generations.get(row["id"]),
                row["repo_id"], requests.get(row["id"]))

    async def _observe_adoption(self, repo, target_ref, head_sha, identities):
        """Name each task's current branch head and whether *head_sha* contains it."""
        async with self.read_snapshot(repo, target_ref) as truth:
            if truth.error and truth.error != "missing_target":
                raise ValueError(f"target could not be observed: {truth.error}")
            if truth.target_oid != head_sha:
                raise ValueError("target ref is no longer at the supplied SHA")
            requests = await load_delivery_requests(
                self.db, identities, repository_id=repo.id, target_ref=target_ref,
            )
            manifest = []
            for task_id, (_status, branch, _epoch, _generation, _repo_id, observed) in (
                identities.items()
            ):
                source = truth.source_heads.get(
                    "refs/remotes/origin/" + branch.removeprefix("refs/heads/")
                ) if branch else None
                request = requests.get(task_id)
                if request != observed:
                    raise DevelopmentBusy("a selected completion changed; adopt again")
                if request and request.completion_id and _status == "COMPLETED":
                    proof = await truth.evaluate(request)
                    if (
                        request.parent_completion is not None or request.parent_adoption is not None
                    ) and not proof.source_oid:
                        kind = "verified" if request.parent_completion is not None else "adopted"
                        raise ValueError(f"{task_id}: {kind} parent source could not be observed")
                    if proof.source_oid:
                        source = proof.source_oid
                probe = await self.git.ais_ancestor(
                    truth.store, source, head_sha, strict=True
                ) if source else None
                contained = probe is True
                if not contained:
                    # False is observed non-ancestry; None is a failed or absent probe.
                    logger.warning(
                        "development adoption: %s source %s (branch %s, completion %s) "
                        "is not observed in %s; ancestry probe %s",
                        task_id, source, branch, request and request.completion_id,
                        head_sha, probe,
                    )
                manifest.append({
                    "task_id": task_id,
                    "source_sha": source,
                    "acceptance": "ancestry" if contained else "operator_equivalent",
                })
            return manifest

    async def _record_adoption(
        self, repo, project_id, target_ref, head_sha, manifest, identities, *, reason,
        operator_id,
    ):
        task_ids = sorted(identities)
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
            generations = await self._completion_generations(conn, task_ids)
            requests = await load_delivery_requests(
                self.db, task_ids, repository_id=repo.id, target_ref=target_ref, conn=conn,
            )
            current_repository = (await conn.execute(select(repos).where(
                repos.c.id == repo.id,
            ).with_for_update())).mappings().one_or_none()
            designated = await conn.scalar(select(projects.c.integration_repository_id).where(
                projects.c.id == project_id,
            ).with_for_update())
            if (designated != repo.id or current_repository is None
                or current_repository["project_id"] != project_id
                or current_repository["url"] != repo.url
                or current_repository["default_branch"] != repo.default_branch):
                raise DevelopmentBusy("repository or delivery target changed; adopt again")
            if {row["id"]: self._adoption_fence(row, generations, requests) for row in selected} != (
                identities
            ):
                raise DevelopmentBusy(
                    "a selected task changed after its source was observed; adopt again"
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
            recorded = {t["id"]: str(uuid4()) for t in selected if t["status"] != "COMPLETED"}
            sources = {member["task_id"]: member["source_sha"] for member in manifest}
            evidence = {
                "kind": "operator_accepted",
                "operator_id": operator_id,
                "validation": "operator_decision",
                "conclusion": "not_ci_attested",
            }
            async with self.read_snapshot(repo, target_ref) as truth:
                if truth.target_oid != head_sha:
                    raise DevelopmentBusy("target moved before adoption; observe again")
                provenance = GitProvenance(self.git, truth.store, repository_url=repo.url)
                for member in manifest:
                    task_id = member["task_id"]
                    source = sources[task_id]
                    if not is_valid_git_oid(source):
                        raise ValueError(f"{task_id}: missing exact completion source")
                    generation = recorded.get(task_id) or requests[task_id].completion_id
                    if generation is None:
                        raise ValueError(f"{task_id}: completion generation requires provenance migration")
                    original = CompletedSource(
                        CompletionIdentity(project_id, repo.id, task_id, generation), source,
                    )
                    retained = await provenance.read_completion(original.identity)
                    if retained is None:
                        await provenance.write_completion(original)
                    elif retained["source_oid"] != source:
                        raise ValueError(f"{task_id}: observed source differs from immutable completion")
                    if member["acceptance"] == "operator_equivalent":
                        replacement_base = await provenance.run("merge-base", source, head_sha)
                        await provenance.write_replacement(
                            source_oid=head_sha, base_oid=replacement_base,
                            replaces=[original], authority="operator", reason=reason,
                        )
                if not await truth.is_fresh():
                    raise DevelopmentBusy("target moved during adoption; observe again")
            if target_ref == "refs/heads/" + repo.default_branch:
                evidence = armed_for_branch_cleanup(evidence)
            await conn.execute(
                self._operation_insert(
                    id=identity,
                    project_id=project_id,
                    repository_id=repo.id,
                    target_ref=target_ref,
                    expected_sha=head_sha,
                    prepared_sha=head_sha,
                    state="finished",
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
            for task_id in close_order:
                if task_id in recorded:
                    await conn.execute(
                        insert(task_completion_records).values(
                            id=recorded[task_id],
                            task_id=task_id,
                            outcome="pass",
                            summary=reason,
                            verification="Operator adoption; not CI attested",
                            commits=json.dumps([sources[task_id]]),
                            completed_at=now,
                            notes=f"Adoption operation {identity}; operator {operator_id}",
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
            from src.integration.pr_cleanup import queue_settled_task_prs_on

            if target_ref == "refs/heads/" + repo.default_branch:
                await queue_settled_task_prs_on(
                    self.db, conn, task_ids, reason=f"Adopted at `{head_sha}`. {reason}",
                    principal=operator_id,
                )
        for result in results:
            await self.db.log_blocked_flips(result.flipped)
            await self.db._notify_settled(result.settled)
            await self.db._notify_ready(result.ready)
        from src.integration.pr_cleanup import retry_settled_task_prs

        pr_cleanup = await retry_settled_task_prs(self.db, self.git, task_ids)
        return {
            "outcome": "adopted",
            "id": identity,
            "head_sha": head_sha,
            "manifest": manifest,
            "pr_cleanup": pr_cleanup,
        }

    async def sweep(self, project_id, *, retry=False, recover_child_id=None, _moved=None):
        """Assemble, validate and publish one batch from a fresh target snapshot.

        ``_moved`` is internal: the target this sweep's caller saw move before
        its publication applied. The caller still holds the exclusion, and a
        second movement is left to the next tick rather than chased.

        Every merge the sweep commits carries the project's resolved Git
        identity (git-identity spec); member authors are preserved.
        """
        project = await self.db.get_project(project_id)
        if project is None or project.hierarchical_integration_mode != "development":
            raise ValueError("project is not in development mode")
        with commit_identity(self.git.resolve_commit_identity(project)):
            return await self._sweep(
                project, retry=retry, recover_child_id=recover_child_id, _moved=_moved
            )

    async def _sweep(self, project, *, retry, recover_child_id, _moved):
        project_id = project.id
        policy = DevelopmentPolicy.model_validate(project.hierarchical_integration_policy).checked()
        repo = await self.db.get_repo(project.integration_repository_id)
        # A reconciler-owned target is an expected no-op for the legacy sweep.
        # Check before entering the root ownership guard so its fail-closed
        # refusal does not turn ordinary ownership into a publisher failure.
        # The check inside exclusion remains necessary for concurrent transfers.
        from src.integration.development_adapter import development_subject_owns_target

        if await development_subject_owns_target(
            self.db, project_id, repo.id, "refs/heads/" + repo.default_branch,
        ):
            return {"outcome": "subject_owned", "parked": []}
        async with nullcontext() if _moved else self.exclusion(repo.id):
            # Shadow subjects and an unowned subject leave this path active.
            # The shared writer takes this same repository exclusion, so a
            # target with durable reconciler ownership cannot publish twice.
            from src.integration.development_adapter import development_subject_owns_target

            if await development_subject_owns_target(
                self.db, project_id, repo.id, "refs/heads/" + repo.default_branch,
            ):
                return {"outcome": "subject_owned", "parked": []}
            await self.rebind_foreign_repositories(project_id, repo)
            await self.refresh_dependencies(project_id)
            if not (retry or recover_child_id) and not await self._has_pending_work(
                project_id, repo, now=time.time()
            ):
                await self.stalls.clear(project_id)
                return {"outcome": "idle", "parked": []}
            store = await self.store(repo, fetch=False)
            target = "refs/heads/" + repo.default_branch
            truth = await delivery_snapshot(
                self.git, store, project_id=project_id, repository_id=repo.id,
                repository_url=repo.url, target_ref=target,
            )
            if truth.error:
                raise GitError(truth.error)
            base = truth.target_oid
            await self.reconcile(repo, store)
            await self.run_git(store, "checkout", "--detach", "--force", base)
            history = await self._release_unverified_parks(repo, await self.rows(project_id))
            source_heads = truth.source_heads
            # A park is a conflict with one target's base. A row parked on
            # another target (before a retarget) holds nothing here; the
            # reconciliation below retires it.
            parked = (
                {
                    (m["task_id"], m.get("source_sha"))
                    for r in history
                    if r["state"] == "parked"
                    and r["repository_id"] == repo.id and r["target_ref"] == target
                    for m in r["manifest"]
                    if m["task_id"] != recover_child_id
                }
                if not retry
                else set()
            )
            manifest, conflicts = [], []
            #: Generated files rebuilt while merging each member, by task id.
            regenerated = {}
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
                                _publishable_object_task(tasks),
                                *([tasks.c.id == isolated_child] if isolated_child else []),
                            )
                            .order_by(tasks.c.updated_at, tasks.c.id)
                        )
                    )
                    .mappings()
                    .all()
                )
            # Inspect every completion independently before gates, sorting,
            # cycles or repair parking. SQL receipts never decide candidacy.
            requests = await load_delivery_requests(
                self.db, {task["id"] for task in candidates},
                repository_id=repo.id, target_ref=target,
            )
            evaluated = await truth.evaluate_many(requests.values())
            own_truth = {task["id"]: evaluated[task["id"]] for task in candidates}
            if await self._settle_not_owed(repo, truth, requests, own_truth,
                                           {task["id"]: task for task in candidates},
                                           history, target):
                requests = await load_delivery_requests(
                    self.db, {task["id"] for task in candidates},
                    repository_id=repo.id, target_ref=target,
                )
                evaluated = await truth.evaluate_many(requests.values())
                own_truth = {task["id"]: evaluated[task["id"]] for task in candidates}
            contained = {
                task_id: evidence for task_id, evidence in own_truth.items()
                if evidence.state is DeliveryState.CONTAINED
            }
            # Nothing to publish: no artifact, or not owed to this target.
            nothing_owed = {
                task_id for task_id, evidence in own_truth.items()
                if evidence.state in {DeliveryState.NO_ARTIFACT, DeliveryState.SETTLED}
            }
            await self.stalls.observe(
                SweepObservation(
                    project_id=project_id, repository_id=repo.id, target_ref=target,
                    target_sha=base,
                    branches={task["id"]: task["branch_name"] for task in candidates},
                    source_heads=source_heads, history=history,
                    pending=frozenset(own_truth),
                ),
                set(contained) | nothing_owed, {},
            )
            async with self.db._engine.connect() as conn:
                eligible_ids = set((await conn.execute(
                    select(tasks.c.id).where(
                        tasks.c.id.in_(requests),
                        ~blocked_predicate(),
                    )
                )).scalars())
            obsolete_ids = set(await self.db.obsolete_task_ids(requests))
            candidates = [task for task in candidates if (
                task["id"] not in contained and task["id"] not in nothing_owed
                and task["id"] not in obsolete_ids and task["id"] in eligible_ids
            )]
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
                sources = _carried_sources(newest, repair_sources)
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
            # Only a prerequisite outside this candidate set needs its own
            # truth: candidates are ordered before their dependents and publish
            # in this batch. A parent orders nothing, so no ancestor is read.
            artifact_ids = dependency_ids - candidate_ids
            artifact_requests = await load_delivery_requests(
                self.db, artifact_ids, repository_id=repo.id, target_ref=target,
            )
            artifact_truth = await truth.evaluate_many(artifact_requests.values())
            all_truth = {**own_truth, **artifact_truth}
            obsolete_artifacts = set(await self.db.obsolete_task_ids(artifact_ids))

            def requires_publication(task_id):
                if task_id in obsolete_artifacts:
                    return False
                evidence = all_truth.get(task_id)
                return evidence is None or not evidence.satisfied

            head = base
            # Only what this batch publishes releases a dependent. A skipped
            # candidate holds its own dependents and nothing else: no sibling,
            # parent or unrelated candidate inherits its skip.
            published = set()
            merged = []  # (task id, source) whose merge moved the aggregate
            parked_ids = {task_id for task_id, _source in parked}
            processed, skipped = set(cycle_blocked), {
                task_id: ("dependency_cycle", dependency_id)
                for task_id, dependency_id in cycle_blocked.items()
            }
            for task_id, dependency_id in sorted(cycle_blocked.items()):
                logger.warning(
                    "development publisher: skipping %s: dependency cycle with %s",
                    task_id, dependency_id,
                )
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
                held = sorted(
                    dependency_id for dependency_id in dependencies.get(task["id"], set())
                    if dependency_id not in published and requires_publication(dependency_id)
                )
                if held:
                    for dependency_id in held:
                        dependency_reason = skipped.get(dependency_id, (None, None))[0]
                        detail = _DEPENDENCY_SKIP_DETAIL.get(
                            dependency_reason, _DEPENDENCY_SKIP_DETAIL["undelivered_dependency"]
                        )
                        logger.warning(
                            "development publisher: skipping %s: %s %s",
                            task["id"], detail, dependency_id,
                            extra={
                                "candidate_task_id": task["id"],
                                "dependency_task_id": dependency_id,
                                "project": project_id,
                            },
                        )
                    first_reason = skipped.get(held[0], (None, None))[0]
                    skipped[task["id"]] = (
                        first_reason if first_reason in _DEPENDENCY_SKIP_DETAIL
                        else "undelivered_dependency", held[0],
                    )
                    continue
                evidence = own_truth[task["id"]]
                # PENDING already proves the source is absent from this base.
                source = evidence.source_oid if (
                    evidence.state is DeliveryState.PENDING
                ) else None
                if not source:
                    skipped[task["id"]] = (
                        "source_parked" if task["id"] in parked_ids else
                        delivery_skip_reason(evidence.reason),
                        task["id"],
                    )
                    logger.warning(
                        "development publisher: skipping %s: %s (completion %s, target %s)",
                        task["id"], evidence.reason, evidence.request.completion_id, base,
                    )
                    continue
                if (task["id"], source) in parked:
                    skipped[task["id"]] = ("source_parked", None)
                    continue
                member = {
                    "task_id": task["id"],
                    "source_sha": source,
                    "parent_task_id": task["parent_task_id"],
                }
                # Every clean member goes straight into the batch aggregate.
                # Grouping by parent is not required, so one member's conflict
                # never holds a sibling.
                await self.run_git(store, "checkout", "--detach", "--force", head)
                outcome = await self.merge_member(store, source, policy)
                if not outcome.ok:
                    await self.run_git(store, "checkout", "--detach", "--force", head)
                    attribution = await self._conflict_evidence(
                        store, base=base, head=head, source=source, merged=merged,
                        regenerate=bool(policy.regenerate),
                    )
                    now = time.time()
                    conflict = {
                        "kind": "merge_conflict", "detail": outcome.detail[-4000:],
                        "conflicting_files": list(outcome.conflicting_files),
                        **({"regenerate": policy.regenerate}
                           if policy.regenerate else {}),
                        **({"regeneration": outcome.regeneration}
                           if outcome.regeneration else {}),
                        "source_sha": source, "target_ref": target,
                        "target_sha": base, "aggregate_sha": head, **attribution,
                    }
                    # ``--retry`` and ``--recover-child`` merge parked content
                    # again.  A repeat conflict refreshes the row that already
                    # names this exact source, whose repair (or its chain) is
                    # still the one working on it; a second row would only
                    # look like a new park that nobody was repairing.
                    reparked = next((
                        row for row in reversed(history)
                        if row["state"] == "parked" and row["repository_id"] == repo.id
                        and row["target_ref"] == target and row["manifest"] == [member]
                    ), None)
                    if reparked is not None:
                        await self.change(
                            reparked["id"], expected_sha=base,
                            evidence={**(reparked["evidence"] or {}), **conflict,
                                      "reconflicted_at": now},
                        )
                    else:
                        repair_id = await self._repair_id_for([member], target, history)
                        if repair_id != self._repair_identity([member]):
                            conflict["repair_id"] = repair_id
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
                                "evidence": conflict,
                                "reason": "source conflict; independent work may continue",
                                "created_at": now,
                                "updated_at": now,
                            }
                        )
                    conflicts.append({**member, **attribution})
                    skipped[task["id"]] = ("merge_conflict", None)
                    continue
                merged_head = await self.run_git(store, "rev-parse", "HEAD")
                if merged_head != head:
                    merged.append((task["id"], source))
                    head = merged_head
                if outcome.regenerated:
                    regenerated[task["id"]] = list(outcome.regenerated)
                published.add(task["id"])
                published.update(replacements)
                manifest.append(member)
                for old_id, old_sha in sorted(replacements.items()):
                    manifest.append({"task_id": old_id, "source_sha": old_sha,
                                     "superseded_by": task["id"]})
                    processed.add(old_id)
                if len(manifest) >= policy.max_batch_size:
                    break
            await self.stalls.observe(
                SweepObservation(
                    project_id=project_id, repository_id=repo.id, target_ref=target,
                    target_sha=base,
                    branches={t: candidate_by_id[t]["branch_name"] for t in processed},
                    source_heads=source_heads, history=history,
                    pending=frozenset(candidate_ids),
                ),
                processed, skipped,
                keep=None if recover_child_id else candidate_ids,
                fresh=processed if (retry or recover_child_id) else (),
            )
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
            if regenerated:
                evidence["regenerated"] = regenerated
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
                repair_id = await self._repair_id_for(manifest, target, history)
                if repair_id != self._repair_identity(manifest):
                    evidence = {**evidence, "repair_id": repair_id}
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
                repo, store, target, head, base, manifest, evidence, "development batch",
                arm_branch_cleanup=True,
            )
            if result["outcome"] == "base_moved" and not _moved:
                # Nothing was written and nothing failed. Assemble again from a
                # fresh fetch: members the new target contains drop out, and
                # the rest merge onto it; stale history is never forced.
                logger.info(
                    "development publisher: %s moved from %s to %s before publication; "
                    "re-assembling from a fresh snapshot",
                    target, base, result.get("observed_sha"),
                    extra={"project": project_id, "batch": result["id"]},
                )
                return await self.sweep(
                    project_id, retry=retry, recover_child_id=recover_child_id,
                    _moved=result.get("observed_sha") or "absent",
                )
            await self.reconcile_parked(
                repo, store, head if result["outcome"] == "delivered" else base
            )
            return result

    async def recover_child(self, project_id, child_id, *, retry=False):
        """Run a supervised sweep and prove the named child's delivery."""
        task = await self.db.get_task(child_id)
        if (
            task is None or task.project_id != project_id
            or task.status != TaskStatus.COMPLETED or not task.branch_name
        ):
            raise ValueError(f"{child_id} is not a completed source task in {project_id}")
        # Recovery is an explicit retry of this child, selected on its own: an
        # unrelated ancestor, sibling or candidate never joins or holds it.
        result = await self.sweep(project_id, retry=retry, recover_child_id=child_id)
        project = await self.db.get_project(project_id)
        repo = await self.db.get_repo(project.integration_repository_id)
        target = "refs/heads/" + repo.default_branch
        history = await self.rows(project_id)
        # A private snapshot proves the current immutable generation, including
        # exact replacement evidence, independently of past operation outcomes.
        async with self.read_snapshot(repo, target) as truth:
            requests = await load_delivery_requests(
                self.db, [child_id], repository_id=repo.id, target_ref=target,
            )
            evidence = await truth.evaluate(requests[child_id])
            source = evidence.source_oid
            verified = evidence.state is DeliveryState.CONTAINED and await truth.is_fresh()
        if not verified:
            skip = await self.db.get_task_meta(child_id, PUBLISHER_SKIP_KEY)
            if result.get("outcome") == "deferred":
                skip = (
                    f"validation could not finish ({result['reason']}); the batch is "
                    "deferred to the next tick, not parked"
                )
            raise ValueError(
                f"{child_id} remains unpublished after sweep: "
                f"{skip or evidence.reason}"
                + await self._parked_repair_note(repo, target, history, child_id)
            )
        # Only a pair proven by its own merge is reported; a conflict against
        # the target names no sibling.
        sibling_conflicts = set()
        for row in history:
            evidence = row.get("evidence") or {}
            if row["state"] != "parked" or evidence.get("kind") != "merge_conflict":
                continue
            members = {member["task_id"] for member in _manifest_members(row["manifest"])}
            proven = {member["task_id"] for member in evidence.get("conflicting_members") or []}
            if child_id in members:
                sibling_conflicts |= proven
            elif child_id in proven:
                sibling_conflicts |= members
        sibling_conflicts.discard(child_id)
        return {
            **result, "recovered_task_id": child_id, "source_sha": source,
            "sibling_conflicts": sorted(sibling_conflicts),
        }

    async def _parked_repair_note(self, repo, target, history, task_id):
        """Say which repair carries *task_id*'s parked source, or that none does.

        Without it a recovery that re-conflicts answers only ``merge_conflict``,
        which reads as "nothing is repairing this" even while a repair of the
        source's first repair is being worked on.
        """
        parked = [
            row for row in reversed(history)
            if row["state"] == "parked" and row["repository_id"] == repo.id
            and any(m["task_id"] == task_id for m in _manifest_members(row["manifest"]))
        ]
        if not parked:
            return ""
        async with self.db._engine.connect() as conn:
            statuses = await repair_statuses(conn, [repo.project_id])
        results = [
            repair_chain(row["manifest"], history, statuses,
                         repository_id=repo.id, target_ref=target, row=row)
            for row in parked
        ]
        found = next((result for result in results if result["open_repair"]), results[0])
        kind = (parked[results.index(found)]["evidence"] or {}).get("kind") or "parked"
        if found["open_repair"]:
            return (
                f"; its {kind} park is carried by repair {found['open_repair']} "
                f"({found['state']}; chain: {describe_repair_chain(found)})"
            )
        return (
            f"; its {kind} park has no open repair ({found['detail']}; chain: "
            f"{describe_repair_chain(found)}): see `aq doctor --check "
            "integration.development_conflicts_unrepaired`"
        )

    async def reconcile_parked(self, repo, store, accepted_head):
        """Dispatch only failures still unresolved after the whole batch was assembled.

        A later consolidation may contain an earlier conflicting source. Starting
        a worker mid-assembly spends tokens repairing work this batch already fixes.
        Parked rows are durable, so a subsequent sweep resumes dispatch after a crash.
        """
        history = await self._release_unverified_parks(repo, await self.rows(repo.project_id))
        target = "refs/heads/" + repo.default_branch
        history = await self._retire_retargeted_parks(repo, history, target)
        pending = {r["id"]: r for r in history if r["state"] == "parked" and r["manifest"]
                   and r["repository_id"] == repo.id and r["target_ref"] == target}
        if not pending:
            return
        truth = await delivery_snapshot(
            self.git, store, project_id=repo.project_id, repository_id=repo.id,
            repository_url=repo.url, target_ref=target,
        )
        requests = await load_delivery_requests(
            self.db, {member["task_id"] for row in pending.values() for member in row["manifest"]},
            repository_id=repo.id, target_ref=target,
        )
        evaluated = await truth.evaluate_many(requests.values())
        if not await truth.is_fresh():
            raise DevelopmentBusy("target changed during parked operation reconciliation")
        for identity, row in list(pending.items()):
            proofs = [evaluated.get(member["task_id"]) for member in row["manifest"]]
            if not all(
                proof is not None
                and proof.state in {DeliveryState.CONTAINED, DeliveryState.SETTLED}
                and proof.source_oid == member.get("source_sha")
                for proof, member in zip(proofs, row["manifest"])
            ):
                continue
            if all(proof.state is DeliveryState.CONTAINED for proof in proofs):
                evidence = armed_for_branch_cleanup({**row["evidence"],
                    "resolved_at_target": truth.target_oid})
                await self.change(identity, state="finished", evidence=evidence)
            else:
                await self._cancel_settled_park(repo, row, proofs, history, truth.target_oid)
            del pending[identity]
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

    async def _repair_id_for(self, manifest, target, history):
        """The repair id a new park of *manifest* on *target* records.

        A manifest's repair is its digest, unless that repair was filed for
        another target (the same source parked before a retarget): a repair
        built on the old target cannot carry the source here.
        """
        usual = self._repair_identity(manifest)
        existing = await self.resolve_task(usual)
        if existing is None:
            return usual
        built_for = repair_target_of(
            existing.description, await self.db.get_task_meta(usual, REPAIR_EVIDENCE_KEY),
            history,
        )
        if built_for in {None, target}:
            return usual
        return self._repair_identity([*manifest, {"target_ref": target}])

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

    async def _repair_links(self, row, history):
        """The repairs filed for parked *row* and its successors, as they exist."""
        async with self.db._engine.connect() as conn:
            statuses = await repair_statuses(conn, [row["project_id"]])
        chain = repair_chain(row["manifest"], history, statuses,
                             repository_id=row["repository_id"],
                             target_ref=row["target_ref"], row=row)["chain"]
        return [link for link in chain if link["status"] is not None]

    async def _cancel_settled_park(self, repo, row, proofs, history, target_oid):
        """Cancel a park whose members this target does not owe, with its repairs.

        Nothing was delivered and nothing is left to repair: a repair filed
        for the row exists only to bring its members here, so it is settled
        too, and the supervisor hears about any still being worked on.
        """
        repairs = await self._repair_links(row, history)
        members = sorted(proof.request.task_id for proof in proofs)
        records = {
            link["task_id"]: settlement_record(
                target_ref=row["target_ref"], repository_id=repo.id,
                reason=SOURCES_NOT_OWED, completion_id=None, source_oid=None,
                detail=f"repair of {', '.join(members)}, not owed to {row['target_ref']}",
                authority="publisher", evidence={"operation_id": row["id"]},
            )
            for link in repairs
        }
        settled = {
            "members": {proof.request.task_id: proof.reason for proof in proofs
                        if proof.state is DeliveryState.SETTLED},
            "repairs": sorted(records), "at_target": target_oid,
        }
        async with self.db._engine.begin() as conn:
            await write_settlements_on(conn, records)
            await revise_operation_on(conn, row["id"], lambda current: (
                {"state": "cancelled",
                 "evidence": {**(current["evidence"] or {}), "settled": settled}}
                if current["state"] == "parked" else None
            ))
            if records:
                await notify_settlements_on(conn, repo.project_id, row["id"], row["target_ref"],
                                            records)

    async def _retire_retargeted_parks(self, repo, history, target):
        """Cancel this repository's parks on a target it no longer publishes to.

        A park is a conflict with one base of one target; after a retarget it
        describes nothing still owed, and its sources are merged into the new
        target afresh (or parked there with their own row). The repair filed
        for it is left to finish and is then settled as built for another
        target. Returns *history* with the retired rows updated.
        """
        stale = [
            row for row in history
            if row["state"] == "parked" and row["repository_id"] == repo.id
            and row["target_ref"] != target
        ]
        if not stale:
            return history
        now = time.time()
        for row in stale:
            retired = {"reason": "retargeted", "target_ref": target, "at": now,
                       "repair": row_repair_identity(row)}
            async with self.db._engine.begin() as conn:
                await revise_operation_on(conn, row["id"], lambda current, retired=retired: (
                    {"state": "cancelled",
                     "evidence": {**(current["evidence"] or {}), "retired": retired}}
                    if current["state"] == "parked" else None
                ))
            logger.warning(
                "development publisher: retired park %s on %s; %s now publishes to %s",
                row["id"], row["target_ref"], repo.id, target,
                extra={"batch": row["id"], "project": repo.project_id},
            )
        return await self.rows(repo.project_id)

    async def _settle_not_owed(self, repo, truth, requests, own_truth, candidates, history,
                               target):
        """Record pending work this target does not owe; return what was settled.

        Two cases, both after a retarget (:mod:`src.integration.development_settlement`):
        a repair filed to publish to another target, and a generation completed
        before the retarget that the previous target contains (or settled).
        Everything else stays owed, and is merged here as usual. The records,
        one journal row and one supervisor notice commit together.
        """
        pending = {
            task_id: evidence for task_id, evidence in own_truth.items()
            if evidence.state is DeliveryState.PENDING
        }
        if not pending:
            return {}
        for task_id in set(await self.db.obsolete_task_ids(pending)):
            del pending[task_id]
        records = {}
        repairs = sorted(task_id for task_id in pending
                         if task_id.startswith("development-repair-"))
        repair_evidence = (
            await self.db.get_task_meta_bulk(repairs, REPAIR_EVIDENCE_KEY) if repairs else {}
        )
        for task_id in repairs:
            built_for = repair_target_of(
                candidates[task_id].get("description"), repair_evidence.get(task_id), history,
            )
            if built_for and built_for != target:
                records[task_id] = settlement_record(
                    target_ref=target, repository_id=repo.id,
                    reason=REPAIR_FOR_PREVIOUS_TARGET, completion_id=None,
                    source_oid=pending[task_id].source_oid,
                    detail=f"repair filed to publish to {built_for}",
                    authority="retarget", evidence={"built_for": built_for},
                )
        retarget = retarget_of(history, repo.id, target)
        if retarget is not None:
            from_ref = retarget["from_ref"]
            previous_oid = truth.source_heads.get(
                "refs/remotes/origin/" + from_ref.removeprefix("refs/heads/")
            )
            owed_before = [
                replace(requests[task_id], target_ref=from_ref) for task_id in sorted(pending)
                if task_id not in records
                and requests[task_id].completed_at is not None
                and requests[task_id].completed_at <= retarget["at"]
            ]
            if owed_before and not is_valid_git_oid(previous_oid):
                logger.warning(
                    "development publisher: %s was retargeted from %s, which is gone; "
                    "%d earlier completion(s) stay owed to %s",
                    repo.id, from_ref, len(owed_before), target,
                    extra={"project": repo.project_id},
                )
            elif owed_before:
                previous = DeliverySnapshot(
                    truth.git, truth.store, truth.project_id, truth.repository_id,
                    truth.repository_url, from_ref, previous_oid, truth.source_heads,
                )
                answers = await previous.evaluate_many(owed_before)
                for task_id, answer in sorted(answers.items()):
                    if answer.state not in {DeliveryState.CONTAINED, DeliveryState.SETTLED}:
                        continue
                    records[task_id] = settlement_record(
                        target_ref=target, repository_id=repo.id,
                        reason=DELIVERED_TO_PREVIOUS_TARGET,
                        completion_id=requests[task_id].completion_id,
                        source_oid=pending[task_id].source_oid,
                        detail=f"delivered to {from_ref} before the retarget to {target}",
                        authority="retarget", evidence={
                            "previous_target_ref": from_ref,
                            "previous_target_oid": previous_oid,
                            "previous_answer": answer.reason,
                            "retarget_operation_id": retarget["operation_id"],
                        },
                    )
        if not records:
            return {}
        now = time.time()
        operation_id = str(uuid4())
        async with self.db._engine.begin() as conn:
            await write_settlements_on(conn, records)
            await conn.execute(self._operation_insert(
                id=operation_id, project_id=repo.project_id, repository_id=repo.id,
                target_ref=target, expected_sha=truth.target_oid, state="finished",
                manifest=[
                    {"task_id": task_id, "source_sha": record["source_oid"]}
                    for task_id, record in sorted(records.items())
                ],
                evidence={"kind": "settlement", "settlements": records},
                reason=f"not owed to {target}", created_at=now, updated_at=now,
            ))
            await notify_settlements_on(conn, repo.project_id, operation_id, target, records,
                                        now=now)
        logger.warning(
            "development publisher: %d completion(s) not owed to %s: %s",
            len(records), target, ", ".join(sorted(records)),
            extra={"project": repo.project_id, "batch": operation_id},
        )
        return records

    async def settle_parked(self, project_id, operation_id, *, reason, operator_id,
                            dismiss=False):
        """Settle or dismiss one parked publisher operation on a stated reason.

        *settle* (the default) records every member's parked generation, and
        every repair in the row's chain, as not owed to the row's target: the
        publisher never merges them there, their dependents are released and
        no further repair is filed. *dismiss* only withdraws the row: its
        members return to the publisher and are merged again on the next sweep
        (and park again if they still conflict); repairs are left alone.
        Either way the row is cancelled with the decision recorded, under the
        publisher's lock so no sweep is using it.
        """
        if not reason.strip():
            raise ValueError("a reason is required to settle or dismiss a parked delivery")
        project = await self.db.get_project(project_id)
        if project is None or not project.integration_repository_id:
            raise ValueError("project has no development repository")
        repo = await self.db.get_repo(project.integration_repository_id)
        async with self.exclusion(repo.id):
            history = await self.rows(project_id)
            row = next((row for row in history if row["id"] == operation_id), None)
            if row is None or row["repository_id"] != repo.id:
                raise ValueError(f"unknown development operation {operation_id}")
            if row["state"] != "parked":
                return {"outcome": "already_terminal", "id": operation_id,
                        "state": row["state"]}
            members = _manifest_members(row["manifest"])
            repairs = await self._repair_links(row, history)
            now = time.time()
            decision = {"reason": reason.strip(), "operator_id": operator_id, "at": now}
            records = {}
            if not dismiss:
                target = "refs/heads/" + repo.default_branch
                if row["target_ref"] != target:
                    raise ValueError(
                        f"{operation_id} parked on {row['target_ref']}, not on the current "
                        f"target {target}; the next sweep retires it (or dismiss it)"
                    )
                requests = await load_delivery_requests(
                    self.db, {member["task_id"] for member in members},
                    repository_id=repo.id, target_ref=target,
                )
                async with self.read_snapshot(repo, target) as truth:
                    proofs = await truth.evaluate_many(
                        request for request in requests.values() if request.completion_id
                    )
                for member in members:
                    request = requests.get(member["task_id"])
                    proof = proofs.get(member["task_id"])
                    if request is None or proof is None:
                        raise ValueError(
                            f"parked member {member['task_id']} has no completion to settle"
                        )
                    if proof.state is DeliveryState.UNKNOWN:
                        raise ValueError(
                            f"{member['task_id']}'s current completion cannot be read in git "
                            f"({proof.reason}); nothing was settled"
                        )
                    if proof.state is not DeliveryState.CONTAINED and (
                        proof.source_oid != member.get("source_sha")
                    ):
                        raise ValueError(
                            f"{member['task_id']} completed again after it parked; its new "
                            "work is owed. Dismiss this row instead."
                        )
                    records[member["task_id"]] = settlement_record(
                        target_ref=target, repository_id=repo.id,
                        reason=OPERATOR_SETTLED, completion_id=request.completion_id,
                        source_oid=member.get("source_sha"), detail=reason.strip(),
                        authority="operator", operator_id=operator_id,
                        evidence={"operation_id": operation_id}, now=now,
                    )
                for link in repairs:
                    records.setdefault(link["task_id"], settlement_record(
                        target_ref=row["target_ref"], repository_id=repo.id,
                        reason=OPERATOR_SETTLED, completion_id=None, source_oid=None,
                        detail=reason.strip(), authority="operator", operator_id=operator_id,
                        evidence={"operation_id": operation_id}, now=now,
                    ))
                decision["settled"] = sorted(records)
            key = "dismissed" if dismiss else "settled_by"
            async with self.db._engine.begin() as conn:
                await write_settlements_on(conn, records)
                current = await revise_operation_on(conn, operation_id, lambda current: (
                    {"state": "cancelled",
                     "evidence": {**(current["evidence"] or {}), key: decision}}
                    if current["state"] == "parked" else None
                ))
                if current is None or current["state"] != "parked":
                    raise DevelopmentBusy("parked operation changed while settling; run again")
        return {
            "outcome": "dismissed" if dismiss else "settled",
            "id": operation_id,
            "target_ref": row["target_ref"],
            "members": [member["task_id"] for member in members],
            "settled": sorted(records),
            "repairs": repairs,
            "open_repairs": [
                link["task_id"] for link in repairs if link["status"] in OPEN_REPAIR_STATUSES
            ],
        }

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

    async def _merge_tree(self, store, ours, theirs):
        """Return the merged tree and conflicted paths, without touching a checkout."""
        result = await self.git.arun_git_result(
            ["merge-tree", "--write-tree", "--name-only", "--no-messages", ours, theirs],
            cwd=str(store),
        )
        lines = result.stdout.splitlines()
        if result.returncode not in {0, 1} or not lines or not is_valid_git_oid(lines[0]):
            raise GitError(result.stderr or "merge-tree could not merge")
        return lines[0], lines[1:] if result.returncode else []

    async def _conflict_evidence(self, store, *, base, head, source, merged, regenerate=False):
        """Name what *source* conflicts with, each claim proven by its own merge.

        A source that conflicts with the target alone blames no batch member.
        Otherwise each member merged before it that touched a conflicting path
        is merged onto the target as a pair with the source, and only a pair
        that conflicts on its own is named. The rest stays unattributed. With
        *regenerate*, a generated file is rebuilt rather than merged
        (:meth:`merge_member`), so a conflict in one proves nothing.
        """
        async def merge_tree(ours, theirs):
            tree, paths = await self._merge_tree(store, ours, theirs)
            if regenerate and paths:
                generated = await self.generated_paths(store, paths)
                paths = [path for path in paths if path not in generated]
            return tree, paths

        try:
            _tree, against_target = await merge_tree(base, source)
            if against_target:
                return {"conflict_with": "target", "conflicting_members": []}
            _tree, against_batch = await merge_tree(head, source)
            if not against_batch:
                return {"conflict_with": "unproven", "conflicting_members": []}
            proven = []
            for task_id, member_source in merged:
                touched = await self.run_git(
                    store, "diff", "--name-only", f"{base}...{member_source}"
                )
                if not set(touched.splitlines()) & set(against_batch):
                    continue
                tree, alone = await merge_tree(base, member_source)
                if alone:
                    continue
                pair = await self.run_git(
                    store, *self.git.resolve_commit_identity().config_args(),
                    "commit-tree", tree, "-p", base, "-p", member_source,
                    "-m", "development conflict attribution",
                )
                _tree, paths = await merge_tree(pair, source)
                if paths:
                    proven.append(
                        {"task_id": task_id, "source_sha": member_source, "paths": paths}
                    )
        except GitError as exc:
            return {"conflict_with": "unproven", "conflicting_members": [],
                    "attribution_error": str(exc)[-500:]}
        return {"conflict_with": "members" if proven else "batch", "conflicting_members": proven}

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

    async def configure(self, project_id, policy, *, reason, operator_id, protection_guard=None):
        """Enter (or reconfigure) development mode for *project_id*.

        *protection_guard*, given the resolved repository, reads the default
        branch's protection and raises when the publisher's push would be
        refused (App-mode spec §8.2); its reading is returned as evidence.
        """
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
        if policy.validation != "none":
            available = presets(Path(__file__).resolve().parents[2])
            for command in policy.commands:
                try:
                    preset, argv = validation_outcomes.finite_command(command)
                    if preset not in available:
                        raise ValueError(
                            f"development validation preset {preset!r} is unavailable on this server"
                        )
                    validate_args(available[preset], argv, Path.cwd(), 1)
                except JobError as exc:
                    raise ValueError(
                        f"unsupported development validation command {command!r}: {exc}"
                    ) from exc
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
        # A GitHub read: before the exclusion and the project lock.
        reading = await protection_guard(repo) if protection_guard is not None else None
        # A new target: the sweep settles work the old one already has
        # (development_settlement); the row says so for the journal.
        moved_from = previous_target(
            await self.rows(project_id), repo.id, "refs/heads/" + repo.default_branch
        )
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
                self._operation_insert(
                    id=str(uuid4()),
                    project_id=project_id,
                    repository_id=repo.id,
                    target_ref="refs/heads/" + repo.default_branch,
                    state="finished",
                    manifest=[],
                    evidence={
                        "kind": "configuration",
                        "operator_id": operator_id,
                        "policy": policy.model_dump(),
                        **({"retarget": {"from_ref": moved_from,
                                         "to_ref": "refs/heads/" + repo.default_branch}}
                           if moved_from else {}),
                    },
                    reason=reason,
                    created_at=now,
                    updated_at=now,
                )
            )
        self.next_due[project_id] = time.time() + policy.interval_seconds
        result = {
            "outcome": "configured",
            "project_id": project_id,
            "repository_id": repo.id,
            "policy": policy.model_dump(),
        }
        if moved_from:
            result["retarget"] = {"from_ref": moved_from,
                                  "to_ref": "refs/heads/" + repo.default_branch}
        if reading is not None:
            result["evidence"] = {"protection": reading.as_dict()}
        return result

    @parent_engine_guard("operation", outcome="blocked")
    async def cancel_preserving(self, operation_id, *, reason):
        from src.database.tables import integration_batches
        from src.database.tables import integration_promotion_intents as intents
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
            if operation["target_kind"] == "parent":
                # Ending the collection or releasing its fence would remove
                # the authority needed to reconcile an interrupted promotion.
                # New parent reservations take this same operation lock.
                pending = await conn.scalar(
                    select(intents.c.id).where(
                        (intents.c.operation_key == operation_id)
                        | (intents.c.resolution_operation_id == operation_id),
                        intents.c.state.not_in(["committed", "conflict", "superseded"]),
                    ).limit(1)
                )
                if pending is not None:
                    raise DevelopmentBusy(
                        f"reconcile unresolved parent promotion {pending} before cancellation"
                    )
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
                self._operation_insert(
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
                    self._operation_insert(
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
            lines += DevelopmentIntegration._conflict_attribution_text(evidence)
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
