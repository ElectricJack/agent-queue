"""Remote branches delivery leaves behind, and which of them may go.

Every worker task pushes ``aq/<task-id>``; every development batch pushes a
candidate snapshot (``aq/development/<project>/<head>``) and, for children,
parent assemblies (``aq/development/parent/<parent>/<assembly>``); every
development repair pushes its own task branch (``aq/development-repair-<id>``,
sometimes with a ``-wip`` sibling).  The publisher lands *merges*, but GitHub
lists every one of those refs as a live branch until somebody deletes it, and
on 2026-09-21 origin carried 961 of them, 893 already merged.

Two callers delete them, and both ask the same question first — "does
anything still need this branch?" (:func:`live_branch_references`):

* :meth:`src.integration.development.DevelopmentIntegration.collect_delivered_branches`
  after a batch is confirmed on the default branch, for exactly the refs that
  batch's journal names; and
* ``aq doctor --check git.stale_branches [--fix]`` — also what the
  supervisor's stall sweep runs — for everything else
  (:func:`find_stale_branches`): branches whose work is on the default
  branch, ``aq/integration/*`` refs whose owner is released and whose
  operation finished (:func:`released_integration_refs`), and branches of
  FAILED or abandoned tasks 14 days after they went terminal
  (:func:`expired_task_branches`).

Only ``aq/`` branches are ever deleted, never the default branch, ``main`` or
``gh-pages`` (:func:`deletable`, enforced inside :func:`delete_branches`).
Every deletion is restorable: a tip the default branch cannot reach is
bundled first, every branch is logged with its sha before the push, and each
delete is a lease on the head that was observed, so a branch somebody pushed
to in the meantime is never removed.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, or_, select

from src.database.queries.blocked_state import _development_delivery_pending
from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES
from src.database.tables import (
    archived_tasks,
    development_deliveries,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    projects,
    sessions,
    task_branch_origins,
    task_completion_records,
    task_metadata,
    tasks,
)
from src.git.manager import GitError, RemoteRefState
from src.integration.live_operations import ACTIVE_OPERATION_STATES
from src.models import TaskStatus

logger = logging.getLogger(__name__)

#: Only branches in the daemon's own namespace are ever deleted.  A person's
#: ``fix/...`` branch is theirs to tidy, whatever its ancestry says.
TASK_BRANCH_PREFIX = "aq/"
#: Never deleted, whatever else is true (the default branch is added per repo).
PROTECTED_BRANCHES = frozenset({"main", "gh-pages"})
#: Publisher assemblies: candidate snapshots and parent aggregates.
ASSEMBLY_PREFIX = "aq/development/"
#: Legacy integration branches (``IntegrationScheduler._integration_branch``);
#: only rule (a) — released owner, finished operation — lets one go.
INTEGRATION_PREFIX = "aq/integration/"
#: A FAILED or abandoned task's branch is kept this long after it went terminal.
FAILED_BRANCH_KEEP_SECONDS = 14 * 24 * 3600.0
#: Temporary refs a backup bundle is written from, removed right after.
BACKUP_REF_PREFIX = "refs/aq-backup/heads/"

#: Task statuses that can still run, and so may still push to their branch.
LIVE_TASK_STATUSES = (
    TaskStatus.DEFINED.value,
    TaskStatus.READY.value,
    TaskStatus.ASSIGNED.value,
    TaskStatus.IN_PROGRESS.value,
    TaskStatus.WAITING_INPUT.value,
    TaskStatus.PAUSED.value,
)
#: Journal states that still owe work to the revisions their manifest names.
UNSETTLED_DELIVERY_STATES = ("prepared", "publishing", "parked")
#: Legacy integration batches that can still read or write their refs.  The
#: same set ``DevelopmentIntegration.configure`` refuses to switch away from.
ACTIVE_BATCH_LIFECYCLES = (
    "sealing", "sealed", "building", "testing", "repairing", "human_blocked",
    "promoting", "cleanup_pending",
)
#: Promotion intents in these states have finished with their refs.
SETTLED_INTENT_STATES = ("committed", "conflict", "superseded")
#: Refs deleted per ``git push``.


def branch_of(ref: Any) -> str | None:
    """``refs/heads/aq/x`` or ``aq/x`` -> ``aq/x``; anything empty -> ``None``."""
    if not ref or not isinstance(ref, str):
        return None
    name = ref.strip().removeprefix("refs/heads/")
    return name or None


def _manifest(value: Any) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return [m for m in value if isinstance(m, dict) and m.get("task_id")]


async def live_branch_references(conn: Any) -> dict[str, str]:
    """Every branch something still needs, mapped to the first reason found.

    Deliberately fleet-wide and by *name*: task ids are unique, so
    ``aq/<task-id>`` means one task wherever it is listed, and a branch held by
    another project's row is held here too.  Over-holding costs a branch that
    stays; under-holding costs work, so every doubtful row holds.

    Holds:

    * a task that can still run (``LIVE_TASK_STATUSES``), or any task with a
      live session — its branch and ``-wip`` sibling;
    * a COMPLETED task whose development delivery is still pending, including
      one on a foreign repository id — the publisher collects from that branch;
    * every member branch of an unsettled journal row (prepared, publishing,
      parked), the row's own target, and every assembly ref carrying one of
      its members — a parked batch's candidate is what its repair inspects;
    * the source branches an open development repair lists;
    * an ``integration_branch_owners`` row that is not ``released``;
    * a live legacy operation's parent, verifier and repair tasks, an active
      legacy batch's integration branch and member refs, and an unsettled
      promotion intent's target and recovery refs;
    * a ``task_branch_origins`` row that is live in a hierarchy/train project
      (there the origin *is* the delivery branch), or one whose discard is
      still pending (``BranchDiscardService`` owns that delete).
    """
    held: dict[str, str] = {}

    def hold(ref: Any, reason: str, *, wip: bool = False) -> None:
        name = branch_of(ref)
        if name is None:
            return
        held.setdefault(name, reason)
        if wip:
            held.setdefault(name + "-wip", reason)

    async def rows(stmt):
        return (await conn.execute(stmt)).mappings().all()

    for row in await rows(
        select(tasks.c.id, tasks.c.branch_name, tasks.c.status).where(
            tasks.c.branch_name.is_not(None), tasks.c.status.in_(LIVE_TASK_STATUSES)
        )
    ):
        hold(row["branch_name"], f"task {row['id']} is {row['status']}", wip=True)

    for row in await rows(
        select(tasks.c.id, tasks.c.branch_name)
        .select_from(tasks.join(sessions, sessions.c.task_id == tasks.c.id))
        .where(tasks.c.branch_name.is_not(None), sessions.c.state.in_(LIVE_SESSION_STATES))
    ):
        hold(row["branch_name"], f"task {row['id']} has a live session", wip=True)

    for row in await rows(
        select(tasks.c.id, tasks.c.branch_name).where(
            tasks.c.status == TaskStatus.COMPLETED.value,
            _development_delivery_pending(tasks, include_foreign_repos=True),
        )
    ):
        hold(row["branch_name"], f"task {row['id']} is not delivered yet", wip=True)

    # Unsettled journal rows: their members, their targets and the assemblies
    # that carry their members.
    unsettled_members: set[tuple[str, str | None]] = set()
    member_ids: dict[str, str] = {}
    for row in await rows(
        select(development_deliveries).where(
            development_deliveries.c.state.in_(UNSETTLED_DELIVERY_STATES)
        )
    ):
        reason = f"development batch {row['id']} is {row['state']}"
        target = branch_of(row["target_ref"])
        if target and target.startswith(TASK_BRANCH_PREFIX):
            hold(target, reason)  # an assembly publish still in flight
        for member in _manifest(row["manifest"]):
            unsettled_members.add((member["task_id"], member.get("source_sha")))
            member_ids.setdefault(member["task_id"], reason)

    # Open development repairs name their sources in metadata.
    repair = tasks.alias("open_repair")
    for task_id, raw in (
        await conn.execute(
            select(task_metadata.c.task_id, task_metadata.c.value)
            .select_from(task_metadata.join(repair, repair.c.id == task_metadata.c.task_id))
            .where(
                task_metadata.c.key == "development_repair_sources",
                repair.c.status.in_(LIVE_TASK_STATUSES),
            )
        )
    ).all():
        for member in _manifest(raw):
            member_ids.setdefault(member["task_id"], f"open development repair {task_id}")

    if member_ids:
        for task_id, branch in await _branches_of(conn, member_ids):
            hold(branch, member_ids[task_id], wip=True)
        for task_id, reason in member_ids.items():
            hold(f"aq/{task_id}", reason, wip=True)

    if unsettled_members:
        for row in await rows(
            select(
                development_deliveries.c.target_ref,
                development_deliveries.c.manifest,
            ).where(development_deliveries.c.target_ref.like("refs/heads/aq/development/%"))
        ):
            carried = {
                (m["task_id"], m.get("source_sha")) for m in _manifest(row["manifest"])
            }
            if carried & unsettled_members:
                hold(row["target_ref"], "assembly carries an undelivered batch member")

    for row in await rows(
        select(
            integration_branch_owners.c.ref,
            integration_branch_owners.c.owner_id,
            integration_branch_owners.c.handoff_state,
        ).where(integration_branch_owners.c.handoff_state != "released")
    ):
        hold(
            row["ref"],
            f"integration owner {row['owner_id']} is {row['handoff_state']}",
            wip=True,
        )

    # Live legacy operations: every task they can still dispatch or verify.
    operation_tasks: dict[str, str] = {}
    for row in await rows(
        select(
            integration_repair_operations.c.id,
            integration_repair_operations.c.parent_task_id,
            integration_repair_operations.c.verifier_task_id,
        ).where(integration_repair_operations.c.state.in_(ACTIVE_OPERATION_STATES))
    ):
        reason = f"integration operation {row['id']} is live"
        for task_id in (row["parent_task_id"], row["verifier_task_id"]):
            if task_id:
                operation_tasks.setdefault(task_id, reason)
        for (task_id,) in (
            await conn.execute(
                select(integration_repair_stages.c.repair_task_id).where(
                    integration_repair_stages.c.operation_id == row["id"],
                    integration_repair_stages.c.repair_task_id.is_not(None),
                )
            )
        ).all():
            operation_tasks.setdefault(task_id, reason)
    if operation_tasks:
        for task_id, branch in await _branches_of(conn, operation_tasks):
            hold(branch, operation_tasks[task_id], wip=True)

    for row in await rows(
        select(integration_batches.c.id, integration_batches.c.integration_branch).where(
            integration_batches.c.lifecycle.in_(ACTIVE_BATCH_LIFECYCLES)
            | (
                (integration_batches.c.lifecycle == "promoted")
                & (integration_batches.c.cleanup_state != "complete")
            )
        )
    ):
        reason = f"integration batch {row['id']} is active"
        hold(row["integration_branch"], reason)
        members = (
            await conn.execute(
                select(
                    integration_batch_members.c.task_id,
                    integration_batch_members.c.source_ref,
                ).where(integration_batch_members.c.batch_id == row["id"])
            )
        ).all()
        for task_id, source_ref in members:
            hold(source_ref, reason)
            hold(f"aq/{task_id}", reason)

    for row in await rows(
        select(
            integration_promotion_intents.c.id,
            integration_promotion_intents.c.target_branch,
            integration_promotion_intents.c.recovery_ref,
        ).where(integration_promotion_intents.c.state.not_in(SETTLED_INTENT_STATES))
    ):
        reason = f"promotion intent {row['id']} is unsettled"
        hold(row["target_branch"], reason)
        hold(row["recovery_ref"], reason)

    for row in await rows(
        select(task_branch_origins.c.task_id, task_branch_origins.c.discard_state)
        .select_from(
            task_branch_origins.outerjoin(tasks, tasks.c.id == task_branch_origins.c.task_id)
            .outerjoin(projects, projects.c.id == tasks.c.project_id)
        )
        .where(
            or_(
                task_branch_origins.c.discard_state == "pending",
                task_branch_origins.c.retired_at.is_(None)
                & projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
            )
        )
    ):
        hold(
            f"aq/{row['task_id']}",
            "branch discard is pending"
            if row["discard_state"] == "pending"
            else f"hierarchy branch origin of {row['task_id']} is live",
        )
    return held


async def _branches_of(conn: Any, task_ids: Iterable[str]) -> list[tuple[str, str]]:
    """``(task_id, branch_name)`` for *task_ids*, live table first, then archive."""
    ids = sorted(set(task_ids))
    found: dict[str, str] = {}
    for table in (tasks, archived_tasks):
        for task_id, branch in (
            await conn.execute(
                select(table.c.id, table.c.branch_name).where(
                    table.c.id.in_(ids), table.c.branch_name.is_not(None)
                )
            )
        ).all():
            found.setdefault(task_id, branch)
    return sorted(found.items())


# -- remote state --------------------------------------------------------


async def remote_heads(run_git, store) -> dict[str, str]:
    """Branch -> head for every remote-tracking ref of ``origin`` in *store*.

    *store* was fetched with ``--prune`` by :meth:`DevelopmentIntegration.store`
    a moment earlier, so this is the remote as of that fetch.
    """
    listed = await run_git(
        store, "for-each-ref", "--format=%(refname) %(objectname)", "refs/remotes/origin/"
    )
    heads = {}
    for line in listed.splitlines():
        ref, _, sha = line.partition(" ")
        name = ref.removeprefix("refs/remotes/origin/")
        if name and name != "HEAD" and sha:
            heads[name] = sha
    return heads


def deletable(branch: str, default_branch: str) -> bool:
    """The one rule no caller can override: ``aq/`` only, never a protected name."""
    return (
        branch.startswith(TASK_BRANCH_PREFIX)
        and branch != default_branch
        and branch not in PROTECTED_BRANCHES
    )


async def delete_branches(
    git,
    run_git,
    store,
    targets: dict[str, dict[str, str]],
    *,
    default_branch: str,
    main_head: str,
    backup_dir: Path,
    repository_id: str,
    now: float | None = None,
) -> dict[str, Any]:
    """Back up, record, then delete each ``branch -> {"head", "reason"}`` from ``origin``.

    Nothing is deleted that cannot be put back:

    1. every tip that is not reachable from *main_head* is written to a new,
       verified bundle under ``<backup_dir>/<yyyy-mm>/`` (thin: ``--not
       <main_head>``, so it restores into any clone of the repository);
    2. every branch — reachable or not — is appended to the month's
       deletion log ``<backup_dir>/<yyyy-mm>.tsv`` as ``branch, sha, reason,
       bundle, recorded_at, repository`` *before* anything is pushed;
    3. each delete carries ``--force-with-lease=<ref>:<head>``, so a branch
       that moved since it was observed is left alone.

    Restore one: ``git bundle unbundle <bundle>`` in any clone (skip it when
    the log says ``-``: the commit is on the default branch), then
    ``git push origin <sha>:refs/heads/<branch>``.

    The push result is not trusted on its own: the remote is listed
    afterwards and each branch is classified from what is there — ``deleted``
    (gone, including already gone), ``moved`` (present at another head: keep)
    or ``failed`` (still at the head: retry).  A branch outside ``aq/`` or a
    protected name raises before anything is written.
    """
    if not targets:
        return {"outcomes": {}, "bundle": None, "bundled": [], "log": None}
    refused = sorted(b for b in targets if not deletable(b, default_branch))
    if refused:
        raise ValueError(f"refusing to delete protected or non-aq/ branches: {refused}")
    if not main_head:
        raise ValueError(f"cannot back up branches without the {default_branch} head")
    now = time.time() if now is None else now
    ordered = sorted(targets)
    unreachable = [
        b for b in ordered
        if not await git.ais_ancestor(str(store), targets[b]["head"], main_head)
    ]
    bundle = (
        await _bundle(run_git, store, {b: targets[b]["head"] for b in unreachable},
                      main_head=main_head, backup_dir=backup_dir,
                      repository_id=repository_id, now=now)
        if unreachable else None
    )
    log = _record_deletions(
        backup_dir, targets, bundled=set(unreachable), bundle=bundle,
        repository_id=repository_id, now=now,
    )
    outcomes = {}
    for branch in ordered:
        try:
            await git.adelete_remote_ref_exact(
                str(store), branch, targets[branch]["head"]
            )
        except GitError:
            # A moved ref or uncertain transfer is resolved by the exact read.
            pass
        observed = await git.als_remote_ref(str(store), branch)
        if observed.state is RemoteRefState.ERROR:
            raise GitError(observed.error or "remote cleanup state is unknown")
        current = observed.oid
        if current is None:
            outcomes[branch] = "deleted"
        elif current == targets[branch]["head"]:
            outcomes[branch] = "failed"
        else:
            outcomes[branch] = "moved"
    return {
        "outcomes": outcomes,
        "bundle": str(bundle) if bundle else None,
        "bundled": unreachable,
        "log": str(log),
    }


def _month(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).strftime("%Y-%m")


async def _bundle(
    run_git, store, heads: dict[str, str], *, main_head, backup_dir, repository_id, now
) -> Path:
    """Write *heads* to a new verified bundle; raise rather than return an unproved one."""
    stamp = datetime.fromtimestamp(now, UTC).strftime("%Y%m%dT%H%M%S%fZ")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", repository_id) or "repository"
    directory = Path(backup_dir) / _month(now)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{stamp}-{slug}.bundle"
    refs = {f"{BACKUP_REF_PREFIX}{branch}": sha for branch, sha in heads.items()}
    try:
        for ref, sha in refs.items():
            await run_git(store, "update-ref", ref, sha)
        await run_git(store, "bundle", "create", str(path), *sorted(refs), "--not", main_head)
        await run_git(store, "bundle", "verify", str(path))
        listed = await run_git(store, "bundle", "list-heads", str(path))
    finally:
        for ref in refs:
            try:
                await run_git(store, "update-ref", "-d", ref)
            except GitError:
                logger.warning("could not remove temporary backup ref %s", ref)
    present = {tuple(line.split(" ", 1)) for line in listed.splitlines() if " " in line}
    missing = sorted(ref for ref, sha in refs.items() if (sha, ref) not in present)
    if missing:
        raise GitError(f"branch backup {path} is missing {missing}")
    return path


def _record_deletions(backup_dir, targets, *, bundled, bundle, repository_id, now) -> Path:
    """Append one line per branch to the month's deletion log, durably."""
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{_month(now)}.tsv"
    recorded_at = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")

    def clean(value) -> str:
        return re.sub(r"[\t\r\n]+", " ", str(value))

    lines = [
        "\t".join((
            branch,
            targets[branch]["head"],
            clean(targets[branch].get("reason") or "-"),
            str(bundle) if branch in bundled else "-",
            recorded_at,
            clean(repository_id),
        ))
        + "\n"
        for branch in sorted(targets)
    ]
    with path.open("a", encoding="utf-8") as handle:
        handle.writelines(lines)
        handle.flush()
        os.fsync(handle.fileno())
    return path


# -- rules for what may go -----------------------------------------------


async def released_integration_refs(conn: Any) -> dict[str, str]:
    """``aq/integration/*`` branches rule (a) lets go of, with why.

    A legacy integration branch may go only when it has at least one
    ``integration_branch_owners`` row, every one of them is ``released``,
    and every operation tied to it — an owner that is an operation, or any
    operation on the batch whose ``integration_branch`` it is — has left
    ``active``/``escalated``/``human_required``.  No owner recovery happens
    here: a ``reserved``/``attached``/``handoff_pending`` row simply holds.
    """
    owners: dict[str, list[tuple[str, str]]] = {}
    for ref, owner_id, state in (
        await conn.execute(
            select(
                integration_branch_owners.c.ref,
                integration_branch_owners.c.owner_id,
                integration_branch_owners.c.handoff_state,
            ).where(integration_branch_owners.c.ref.like("%aq/integration/%"))
        )
    ).all():
        branch = branch_of(ref)
        if branch and branch.startswith(INTEGRATION_PREFIX):
            owners.setdefault(branch, []).append((owner_id, state))
    if not owners:
        return {}
    batches: dict[str, list[str]] = {}
    for batch_id, ref in (
        await conn.execute(
            select(integration_batches.c.id, integration_batches.c.integration_branch).where(
                integration_batches.c.integration_branch.is_not(None)
            )
        )
    ).all():
        branch = branch_of(ref)
        if branch in owners:
            batches.setdefault(branch, []).append(batch_id)
    batch_ids = [b for ids in batches.values() for b in ids]
    owner_ids = [o for rows in owners.values() for o, _ in rows]
    operations = (
        await conn.execute(
            select(
                integration_repair_operations.c.id,
                integration_repair_operations.c.batch_id,
                integration_repair_operations.c.state,
            ).where(
                integration_repair_operations.c.id.in_(owner_ids)
                | integration_repair_operations.c.batch_id.in_(batch_ids)
            )
        )
    ).all()
    released = {}
    for branch, rows in sorted(owners.items()):
        if any(state != "released" for _, state in rows):
            continue
        tied = [
            (op_id, state)
            for op_id, batch_id, state in operations
            if op_id in {o for o, _ in rows} or batch_id in batches.get(branch, [])
        ]
        if any(state in ACTIVE_OPERATION_STATES for _, state in tied):
            continue
        detail = ", ".join(f"{op_id} {state}" for op_id, state in sorted(tied)) or "none"
        released[branch] = f"integration owner released; operations: {detail}"
    return released


async def expired_task_branches(
    conn: Any, *, now: float, keep_seconds: float = FAILED_BRANCH_KEEP_SECONDS
) -> dict[str, str]:
    """Branches of FAILED or abandoned tasks terminal for *keep_seconds* (rule b).

    Live and archived tasks both count.  Abandoned means ``work_outcome``
    ``abandoned`` in the task's metadata or its latest completion record.
    The clock starts at the later of the task's ``updated_at`` and its latest
    completion, so any later activity restarts the wait.  A task that can
    still run is never listed (and is held anyway).
    """
    abandoned_meta = set(
        (
            await conn.execute(
                select(task_metadata.c.task_id).where(
                    task_metadata.c.key == "work_outcome",
                    task_metadata.c.value == json.dumps("abandoned"),
                )
            )
        ).scalars()
    )
    latest = (
        select(
            task_completion_records.c.task_id,
            func.max(task_completion_records.c.completed_at).label("completed_at"),
        )
        .group_by(task_completion_records.c.task_id)
        .subquery()
    )
    abandoned_close = set(
        (
            await conn.execute(
                select(task_completion_records.c.task_id)
                .select_from(
                    task_completion_records.join(
                        latest,
                        (latest.c.task_id == task_completion_records.c.task_id)
                        & (latest.c.completed_at == task_completion_records.c.completed_at),
                    )
                )
                .where(task_completion_records.c.work_outcome == "abandoned")
            )
        ).scalars()
    )
    abandoned = abandoned_meta | abandoned_close
    cutoff = now - keep_seconds
    expired: dict[str, str] = {}
    for table, archived in ((tasks, False), (archived_tasks, True)):
        rows = (
            await conn.execute(
                select(
                    table.c.id, table.c.branch_name, table.c.status, table.c.updated_at,
                    latest.c.completed_at,
                )
                .select_from(table.outerjoin(latest, latest.c.task_id == table.c.id))
                .where(
                    table.c.branch_name.is_not(None),
                    (table.c.status == TaskStatus.FAILED.value)
                    | (
                        table.c.id.in_(abandoned)
                        & table.c.status.in_(
                            (TaskStatus.COMPLETED.value, TaskStatus.FAILED.value,
                             TaskStatus.BLOCKED.value)
                        )
                    ),
                )
            )
        ).all()
        for task_id, branch, status, updated_at, completed_at in rows:
            terminal_at = max(float(updated_at or 0), float(completed_at or 0))
            if terminal_at > cutoff:
                continue
            kind = "abandoned" if task_id in abandoned else status
            since = datetime.fromtimestamp(terminal_at, UTC).date().isoformat()
            where = " (archived)" if archived else ""
            reason = f"task {task_id}{where} {kind} since {since}"
            name = branch_of(branch)
            if name:
                expired.setdefault(name, reason)
                expired.setdefault(name + "-wip", reason)
    return expired


# -- the backlog ---------------------------------------------------------


_FIELD = "\x1f"


async def find_stale_branches(
    run_git,
    store,
    *,
    default_branch: str,
    holds: dict[str, str],
    released: dict[str, str] | None = None,
    expired: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Classify every branch of ``origin`` under the branch policy.

    An ``aq/`` branch is *stale* by exactly one rule:

    * ``integration`` — an ``aq/integration/*`` ref in *released* (rule a;
      such a ref is never judged by its ancestry);
    * ``landed`` — its work is on the default branch: the head is an
      ancestor (``ancestry``), or every non-merge commit beyond the default
      branch has a twin there with the same author e-mail, author time and
      subject (``subject``, how a rebased or cherry-picked copy looks); and
      every merge beyond the default branch is *clean* — its tree is exactly
      Git's own merge of its parents — because a hand-resolved merge can hold
      work nothing else has;
    * ``expired`` — the branch of a FAILED or abandoned task in *expired*
      (rule b).

    A stale branch in *holds* is reported under ``held`` instead.  Returns
    ``{"stale": [...], "held": [...], "kept": int, "out_of_scope": int,
    "main_head": sha}``; entries carry ``branch``, ``head`` (to lease the
    delete on), ``rule``, ``found_by`` and ``reason``.  Branches outside
    ``aq/``, the default branch, ``main`` and ``gh-pages`` are only counted.
    """
    released = released or {}
    expired = expired or {}
    heads = await remote_heads(run_git, store)
    main_head = heads.get(default_branch)
    if main_head is None:
        raise ValueError(f"origin has no {default_branch} branch")
    merged = set(
        (
            await run_git(
                store,
                "for-each-ref",
                "--merged",
                main_head,
                "--format=%(refname)",
                "refs/remotes/origin/",
            )
        ).split()
    )
    main_stamps: set[str] | None = None

    async def on_main(branch, head):
        nonlocal main_stamps
        if f"refs/remotes/origin/{branch}" in merged:
            return "ancestry"
        beyond = await run_git(
            store, "log", "--no-merges",
            f"--format=%ae{_FIELD}%at{_FIELD}%s", f"{main_head}..{head}", "--",
        )
        stamps = [line for line in beyond.splitlines() if line]
        if stamps and main_stamps is None:
            main_stamps = set(
                (
                    await run_git(
                        store, "log", "--no-merges",
                        f"--format=%ae{_FIELD}%at{_FIELD}%s", main_head, "--",
                    )
                ).splitlines()
            )
        if all(stamp in main_stamps for stamp in stamps) and await _merges_are_clean(
            run_git, store, main_head, head
        ):
            return "subject" if stamps else "ancestry"
        return None

    stale, held = [], []
    kept = out_of_scope = 0
    for branch, head in sorted(heads.items()):
        if not deletable(branch, default_branch):
            if branch != default_branch:
                out_of_scope += 1
            continue
        entry = None
        if branch.startswith(INTEGRATION_PREFIX):
            if branch in released:
                entry = {"rule": "integration", "found_by": "owner",
                         "reason": released[branch]}
        else:
            how = await on_main(branch, head)
            if how is not None:
                entry = {"rule": "landed", "found_by": how,
                         "reason": f"work is on {default_branch} ({how})"}
            elif branch in expired:
                entry = {"rule": "expired", "found_by": "age", "reason": expired[branch]}
        if entry is None:
            kept += 1
            continue
        entry = {"branch": branch, "head": head, **entry}
        if branch in holds:
            held.append({**entry, "held_by": holds[branch]})
        else:
            stale.append(entry)
    return {
        "stale": stale,
        "held": held,
        "kept": kept,
        "out_of_scope": out_of_scope,
        "main_head": main_head,
    }


async def _merges_are_clean(run_git, store, main_head: str, head: str) -> bool:
    """Whether every merge in ``main_head..head`` is Git's own merge of its parents.

    Needs ``git merge-tree --write-tree`` (Git 2.38+).  Anything that cannot
    be proved clean — a conflict, an octopus, an older Git — is not.
    """
    listed = await run_git(store, "rev-list", "--merges", "--parents", f"{main_head}..{head}", "--")
    for line in listed.splitlines():
        commit, *parents = line.split()
        if len(parents) != 2:
            return False
        try:
            merged = await run_git(store, "merge-tree", "--write-tree", *parents)
            recorded = await run_git(store, "rev-parse", f"{commit}^{{tree}}")
        except GitError:
            return False
        if merged.splitlines()[:1] != [recorded]:
            return False
    return True


__all__ = [
    "ASSEMBLY_PREFIX",
    "FAILED_BRANCH_KEEP_SECONDS",
    "INTEGRATION_PREFIX",
    "PROTECTED_BRANCHES",
    "TASK_BRANCH_PREFIX",
    "branch_of",
    "deletable",
    "delete_branches",
    "expired_task_branches",
    "find_stale_branches",
    "live_branch_references",
    "released_integration_refs",
    "remote_heads",
]
