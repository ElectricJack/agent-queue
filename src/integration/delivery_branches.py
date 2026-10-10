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

* :meth:`src.integration.development.DevelopmentPrimitives.collect_delivered_branches`
  after a batch is confirmed on the default branch, for exactly the refs that
  batch's journal names; and
* ``aq doctor --check git.stale_branches [--fix]`` — also what the
  supervisor's stall sweep runs — for everything else
  (:func:`find_stale_branches`): branches whose work is on the default
  branch, ``aq/integration/*`` refs whose owner is released and whose
  operation finished (:func:`released_integration_refs`), and branches of
  FAILED or abandoned tasks 14 days after they went terminal
  (:func:`expired_task_branches`).

Only ``aq/`` branches are ever deleted, never the default branch, a promotion
target or ``gh-pages`` (:func:`deletable`, enforced inside :func:`delete_branches`).
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

from sqlalchemy import func, null, or_, select

from src.database.queries.hierarchy_queries import LIVE_SESSION_STATES
from src.database.tables import (
    archived_tasks,
    branch_retirements,
    integration_batch_members,
    integration_batches,
    integration_branch_owners,
    integration_legacy_deliveries,
    integration_promotion_intents,
    integration_repair_operations,
    integration_repair_stages,
    integration_subjects,
    projects,
    repos,
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
#: Always protected; the default and promotion targets are added per repository.
PROTECTED_BRANCHES = frozenset({"gh-pages"})
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
#: same set ``DevelopmentPrimitives.configure`` refuses to switch away from.
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


async def completed_branch_tasks(conn: Any, branches: Iterable[str]) -> set[str]:
    """COMPLETED tasks in development delivery scope that own one of *branches*.

    These are the tasks :func:`live_branch_references` asks git about; a
    cleanup observes exactly them, for exactly the branches it may delete.
    A ``-wip`` sibling belongs to its task's branch.
    """
    from src.integration.delivery_observer import development_delivery_scope

    names = {branch_of(branch) for branch in branches} - {None}
    names |= {name.removesuffix("-wip") for name in names}
    if not names:
        return set()
    return set(
        (
            await conn.execute(
                select(tasks.c.id).where(
                    tasks.c.status == TaskStatus.COMPLETED.value,
                    tasks.c.branch_name.in_(sorted(names | {"refs/heads/" + n for n in names})),
                    development_delivery_scope(tasks),
                )
            )
        ).scalars()
    )


async def live_branch_references(
    conn: Any, *, delivery: Any = None, retiring_task_id: str | None = None,
    retiring_owner_id: str | None = None,
) -> dict[str, str]:
    """Every branch something still needs, mapped to the first reason found.

    Deliberately fleet-wide and by *name*: task ids are unique, so
    ``aq/<task-id>`` means one task wherever it is listed, and a branch held by
    another project's row is held here too.  Over-holding costs a branch that
    stays; under-holding costs work, so every doubtful row holds.

    Holds:

    * a task that can still run (``LIVE_TASK_STATUSES``), or any task with a
      live session — its branch and ``-wip`` sibling;
    * a COMPLETED task in development delivery scope, including one on a
      foreign repository id, unless *delivery* (a
      :class:`~src.integration.delivery_observer.DeliveryView` taken before
      this read) proves in git that its current work is on the target — the
      publisher collects from that branch, and unknown is never "delivered";
    * every member branch of an unsettled publisher operation (prepared,
      publishing, parked), its own target, and every assembly ref carrying one
      of its members — a parked batch's candidate is what its repair inspects;
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

    from src.integration.delivery_observer import development_delivery_scope
    from src.integration.delivery_truth import DeliveryState

    completed = await rows(
        select(tasks.c.id, tasks.c.branch_name).where(
            tasks.c.status == TaskStatus.COMPLETED.value,
            tasks.c.branch_name.is_not(None),
            development_delivery_scope(tasks),
        )
    )
    verified = (
        await delivery.verified_on(conn, {row["id"] for row in completed})
        if delivery is not None and completed else {}
    )
    for row in completed:
        if row["id"] == retiring_task_id:
            continue  # The explicit retirement decision waives this task's delivery.
        evidence = verified.get(row["id"])
        if evidence is not None and evidence.satisfied:
            continue
        if evidence is not None and evidence.state is DeliveryState.PENDING:
            reason = f"task {row['id']} is not delivered yet"
        else:
            why = evidence.reason if evidence is not None else "not verified in git"
            reason = f"task {row['id']} delivery is unknown ({why})"
        hold(row["branch_name"], reason, wip=True)

    # Publisher operations still in flight (outstanding legacy journal rows
    # were retained as ``legacy-operation:`` events): their members, their
    # targets and the assemblies that carry their members.
    from src.integration.development import operation_rows_on

    operations = await operation_rows_on(conn)
    unsettled_members: set[tuple[str, str | None]] = set()
    member_ids: dict[str, str] = {}
    for row in operations:
        if row["state"] not in UNSETTLED_DELIVERY_STATES:
            continue
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
        for row in operations:
            if not str(row.get("target_ref") or "").startswith("refs/heads/" + ASSEMBLY_PREFIX):
                continue
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
        if row["owner_id"] == retiring_owner_id:
            continue
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
        select(
            task_branch_origins.c.task_id,
            task_branch_origins.c.branch_name,
            task_branch_origins.c.discard_state,
        )
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
        if row["task_id"] == retiring_task_id:
            continue
        hold(
            row["branch_name"],
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

    *store* was fetched with ``--prune`` by :meth:`DevelopmentPrimitives.store`
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


async def train_cleanup_hold(conn, *, repository_id: str, ref: str) -> str | None:
    """Protect current writers and open collection targets during train cleanup.

    A completed origin alone is not a hold: the caller separately proves the
    exact remote tip reachable in Git and deletes under a managed ref lease.
    """
    name = branch_of(ref)
    aliases = (name, f"refs/heads/{name}")
    for table in (tasks, archived_tasks):
        live = await conn.scalar(select(table.c.id).where(
            table.c.repo_id == repository_id, table.c.branch_name.in_(aliases),
            table.c.status.in_(LIVE_TASK_STATUSES),
        ).limit(1))
        if live:
            return f"task {live} can still run"
    session = await conn.scalar(select(tasks.c.id).join(
        sessions, sessions.c.task_id == tasks.c.id,
    ).where(tasks.c.repo_id == repository_id, tasks.c.branch_name.in_(aliases),
            sessions.c.state.in_(LIVE_SESSION_STATES)).limit(1))
    if session:
        return f"task {session} has a live session"
    target = await conn.scalar(select(integration_batches.c.id).where(
        integration_batches.c.repository_id == repository_id,
        integration_batches.c.lifecycle.in_(ACTIVE_BATCH_LIFECYCLES),
        or_(integration_batches.c.target_ref == ref,
            integration_batches.c.integration_branch == ref),
    ).limit(1))
    if target:
        return f"batch {target} still needs this ref"
    for table in (tasks, archived_tasks):
        member = await conn.scalar(select(integration_batch_members.c.task_id)
            .join(integration_batches,
                  integration_batches.c.id == integration_batch_members.c.batch_id)
            .outerjoin(table, table.c.id == integration_batch_members.c.task_id)
            .where(integration_batches.c.repository_id == repository_id,
                   integration_batches.c.lifecycle.in_(ACTIVE_BATCH_LIFECYCLES),
                   or_(integration_batch_members.c.source_ref == ref,
                       table.c.branch_name.in_(aliases))).limit(1))
        if member:
            return f"active batch still needs member {member}"
    subject = await conn.scalar(select(integration_subjects.c.id).where(
        integration_subjects.c.repository_id == repository_id,
        integration_subjects.c.target_ref == ref,
        integration_subjects.c.phase != "done",
    ).limit(1))
    if subject:
        return f"subject {subject} still targets this ref"
    discard = await conn.scalar(select(task_branch_origins.c.task_id).where(
        task_branch_origins.c.repository_id == repository_id,
        task_branch_origins.c.branch_name.in_(aliases),
        task_branch_origins.c.discard_state == "pending",
    ).limit(1))
    return f"branch discard of {discard} is pending" if discard else None


async def preserve_branch_tips(
    run_git, store, heads: dict[str, str], *, target_sha: str, backup_dir: Path,
    repository_id: str, now: float,
) -> Path:
    """Bundle unreachable tips without deleting or moving their branches."""
    return await _bundle(run_git, store, heads, main_head=target_sha, backup_dir=backup_dir,
                         repository_id=repository_id, now=now)




def protected_branches(default_branch: str, promotion_flow=None) -> frozenset[str]:
    """All flow targets stay protected, including ones in the ``aq/`` namespace."""
    # The promotion lane builds on gitops -> development -> this module.
    from src.integration.promotion_steps import flow_targets

    return (PROTECTED_BRANCHES | {branch_of(default_branch)} | {
        branch_of(target) for target in flow_targets(promotion_flow)
    }) - {None}


async def repository_protected_branches(
    conn, repository_id: str, *, default_branch: str,
) -> frozenset[str]:
    row = (await conn.execute(
        select(repos.c.default_branch, projects.c.promotion_flow)
        .select_from(repos.join(projects, projects.c.id == repos.c.project_id))
        .where(repos.c.id == repository_id)
    )).one_or_none()
    protected = protected_branches(default_branch)
    if row is not None:
        protected |= protected_branches(row.default_branch, row.promotion_flow)
    return protected


def deletable(branch: str, default_branch: str, *, protected: Iterable[str] = ()) -> bool:
    """The one rule no caller can override: ``aq/`` only, never a protected name."""
    return (
        branch.startswith(TASK_BRANCH_PREFIX)
        and branch != default_branch
        and branch not in PROTECTED_BRANCHES
        and branch not in protected
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
    protected: Iterable[str] = (),
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
    protected = frozenset(protected)
    refused = sorted(b for b in targets if not deletable(b, default_branch, protected=protected))
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


# -- completed work the default branch cannot reach -----------------------

#: How long a COMPLETED root's pull request may wait for delivery before it is
#: called stranded rather than queued.  A train lane can hold a root for hours
#: (fleet-ridge-45 waited 67 minutes behind one dev batch); a day is not a queue.
ROOT_DELIVERY_WAIT_SECONDS = 24 * 3600.0
#: Integration modes in which aq delivers a project's work; elsewhere an
#: unmerged task branch is expected.
DELIVERING_MODES = ("hierarchy", "train", "development")
#: Parent links followed when asking whether an ancestor's branch carries work.
_ANCESTOR_DEPTH = 8


def _day(stamp: float | None) -> str:
    return datetime.fromtimestamp(float(stamp or 0), UTC).strftime("%Y-%m-%d %H:%M UTC")


async def completed_branch_dispositions(
    conn: Any,
    *,
    heads: dict[str, str],
    reachable: Any,
    contains: Any,
    delivered: Any = None,
    now: float,
    project_id: str | None = None,
    repository_id: str | None = None,
    task_ids: Any = None,
    wait_seconds: float = ROOT_DELIVERY_WAIT_SECONDS,
) -> list[dict[str, Any]]:
    """Why each COMPLETED task's branch the default branch cannot reach is still there.

    *heads* is ``branch -> head`` on origin.  Three probes answer the git
    questions, so only branches a COMPLETED task names are ever probed:
    ``await reachable(head)`` — whether the default branch reaches the tip
    (``None`` when that cannot be read); ``await contains(head, branch)`` —
    whether another origin branch reaches it; and, optionally,
    ``await delivered(task_ids)`` — the tasks the delivery truth proves are on
    the default branch some other way (a train collection, a recorded proof).

    Every branch named by a COMPLETED task (live or archived, its ``-wip``
    sibling too; of *project_id*, on *repository_id* or no repository, and
    among *task_ids*, when given) whose tip the default branch does not reach
    gets one entry,
    ``accounted`` when something on record explains it or will move it, a
    finding when nothing will:

    * ``retiring`` — a branch retirement is pending (archive and every recorded
      disposition queue one); ``retirement_conflict`` — it stopped and needs a
      person (a finding);
    * ``carried`` — a live batch has the task as a member;
    * ``recorded`` — an ``integration_legacy_deliveries`` row records its
      delivery, re-landing or abandonment;
    * ``delivered`` — the delivery truth proves the work landed;
    * ``integrated`` — an ancestor task's branch on origin contains the tip, so
      the work travels with that ancestor (which is judged on its own);
    * ``awaiting_delivery`` — a root with a pull request, completed less than
      *wait_seconds* ago; past that it is ``stranded_root`` (a finding);
    * ``awaiting_backstop`` — archived less than twice *wait_seconds* ago with
      no retirement yet: the daily Git sweep queues one for every archived
      task; past that it is ``unreconciled``;
    * ``unreconciled`` — none of the above (a finding);
    * ``unknown`` — reachability could not be read (neither accounted nor a
      finding).
    """
    names = {branch.removesuffix("-wip") for branch in heads}
    stored = names | {"refs/heads/" + name for name in names}
    candidates: dict[str, tuple[dict, bool, bool]] = {}
    for table, archived in ((tasks, False), (archived_tasks, True)):
        scope = [
            table.c.status == TaskStatus.COMPLETED.value,
            table.c.branch_name.in_(sorted(stored)),
        ]
        if project_id is not None:
            scope.append(table.c.project_id == project_id)
        if repository_id is not None:
            scope.append(table.c.repo_id.is_(None) | (table.c.repo_id == repository_id))
        if task_ids is not None:
            scope.append(table.c.id.in_(sorted(task_ids)))
        rows = await conn.execute(
            select(
                table.c.id, table.c.project_id, table.c.branch_name,
                table.c.parent_task_id, table.c.pr_url, table.c.updated_at,
                (table.c.archived_at if archived else null()).label("archived_at"),
            ).where(*scope)
        )
        for row in rows.mappings():
            name = branch_of(row["branch_name"])
            for branch, wip in ((name, False), (f"{name}-wip", True)):
                if branch in heads:
                    candidates.setdefault(branch, (dict(row), archived, wip))
    owners: dict[str, tuple[dict, bool, bool]] = {}
    entries: list[dict[str, Any]] = []
    for branch, (row, archived, wip) in sorted(candidates.items()):
        answer = await reachable(heads[branch])
        if answer is None:
            entries.append(_disposition(
                row, archived, branch, heads[branch], wip, "unknown",
                "the default branch's reachability of this tip cannot be read",
            ))
        elif not answer:
            owners[branch] = (row, archived, wip)
    if not owners:
        return entries
    ids = sorted({row["id"] for row, _, _ in owners.values()})

    retirements: dict[str, dict] = {}
    for row in (
        await conn.execute(
            select(
                branch_retirements.c.task_id, branch_retirements.c.branch,
                branch_retirements.c.state, branch_retirements.c.reason,
                branch_retirements.c.last_error, branch_retirements.c.requested_at,
            )
            .where(
                branch_retirements.c.task_id.in_(ids)
                | branch_retirements.c.branch.in_(sorted(owners))
            )
            .order_by(branch_retirements.c.requested_at)
        )
    ).mappings():
        retirements[row["branch"]] = dict(row)  # the latest request wins
        if row["task_id"]:
            retirements[f"task:{row['task_id']}"] = dict(row)
    batches = {
        task_id: (batch_id, lifecycle, target)
        for task_id, batch_id, lifecycle, target in (
            await conn.execute(
                select(
                    integration_batch_members.c.task_id, integration_batches.c.id,
                    integration_batches.c.lifecycle, integration_batches.c.target_ref,
                )
                .select_from(
                    integration_batch_members.join(
                        integration_batches,
                        integration_batches.c.id == integration_batch_members.c.batch_id,
                    )
                )
                .where(
                    integration_batch_members.c.task_id.in_(ids),
                    integration_batches.c.lifecycle.in_(ACTIVE_BATCH_LIFECYCLES),
                )
                .order_by(integration_batches.c.created_at)
            )
        ).all()
    }
    proofs = {
        row["task_id"]: row
        for row in (
            await conn.execute(
                select(
                    integration_legacy_deliveries.c.task_id,
                    integration_legacy_deliveries.c.proof,
                    integration_legacy_deliveries.c.delivered_sha,
                ).where(integration_legacy_deliveries.c.task_id.in_(ids))
            )
        ).mappings()
    }
    completed_at = dict(
        (
            await conn.execute(
                select(
                    task_completion_records.c.task_id,
                    func.max(task_completion_records.c.completed_at),
                )
                .where(task_completion_records.c.task_id.in_(ids))
                .group_by(task_completion_records.c.task_id)
            )
        ).all()
    )
    parents: dict[str, tuple[str | None, str | None]] = {}
    frontier = {row["parent_task_id"] for row, _, _ in owners.values()} - {None}
    for _ in range(_ANCESTOR_DEPTH):
        frontier -= set(parents)
        if not frontier:
            break
        for table in (tasks, archived_tasks):
            for task_id, parent_id, branch in (
                await conn.execute(
                    select(table.c.id, table.c.parent_task_id, table.c.branch_name).where(
                        table.c.id.in_(sorted(frontier))
                    )
                )
            ).all():
                parents.setdefault(task_id, (parent_id, branch_of(branch)))
        frontier = {parent for parent, _ in parents.values()} - {None}
    proven = set(await delivered(ids)) if delivered is not None else set()

    for branch, (row, archived, wip) in sorted(owners.items()):
        task_id, head = row["id"], heads[branch]
        retirement = retirements.get(branch) or retirements.get(f"task:{task_id}")
        rule = reason = None
        if retirement is not None and retirement["state"] == "pending":
            rule, reason = "retiring", (
                f"retirement pending since {_day(retirement['requested_at'])}: "
                f"{retirement['reason']}"
            )
        elif retirement is not None and retirement["state"] == "conflict":
            rule, reason = "retirement_conflict", (
                f"its retirement stopped: {retirement['last_error'] or 'conflict'}"
            )
        elif task_id in batches:
            batch_id, lifecycle, target = batches[task_id]
            rule, reason = "carried", f"batch {batch_id} ({lifecycle}) carries it to {target}"
        elif task_id in proofs:
            proof = proofs[task_id]
            sha = (proof["delivered_sha"] or "")[:12]
            rule, reason = "recorded", (
                f"its delivery is recorded as {proof['proof']}" + (f" at {sha}" if sha else "")
            )
        elif task_id in proven:
            rule, reason = "delivered", "the delivery truth proves its work is on the default branch"
        else:
            ancestor, seen = row["parent_task_id"], set()
            while ancestor and ancestor not in seen and rule is None:
                seen.add(ancestor)
                parent_id, parent_branch = parents.get(ancestor, (None, None))
                if parent_branch in heads and await contains(head, parent_branch):
                    rule, reason = "integrated", (
                        f"{parent_branch} (task {ancestor}) carries its work"
                    )
                ancestor = parent_id
        if rule is None and row["parent_task_id"] is None and row["pr_url"] and not archived:
            since = completed_at.get(task_id) or row["updated_at"]
            if now - float(since or 0) < wait_seconds:
                rule, reason = "awaiting_delivery", (
                    f"root pull request {row['pr_url']} waits for delivery "
                    f"(completed {_day(since)})"
                )
            else:
                rule, reason = "stranded_root", (
                    f"root pull request {row['pr_url']} has waited since {_day(since)} and "
                    f"no batch carries it; `aq integration authorize-root {task_id}` says why"
                )
        if rule is None and archived:
            since = row["archived_at"] or row["updated_at"]
            if now - float(since or 0) < 2 * wait_seconds:
                rule, reason = "awaiting_backstop", (
                    f"archived {_day(since)}; the daily Git sweep queues its retirement"
                )
            else:
                rule, reason = "unreconciled", (
                    f"archived {_day(since)} and no retirement was ever queued"
                )
        if rule is None:
            rule, reason = "unreconciled", (
                "no batch carries it, no delivery or disposition is recorded and no "
                "retirement is queued"
            )
        entries.append(_disposition(row, archived, branch, head, wip, rule, reason))
    return entries


async def observe_completed_branches(
    db: Any,
    observer: Any,
    *,
    project_ids: Any = None,
    task_ids: Any = None,
    now: float | None = None,
    cached_only: bool = False,
) -> dict[str, Any]:
    """:func:`completed_branch_dispositions` over each delivering project's repository.

    One snapshot of each project's integration repository answers the git
    questions, reused when *observer* fetched it within ``READ_MAX_AGE``;
    *cached_only* never fetches (interactive diagnostics), so an expired
    snapshot is ``unavailable``.  A project whose default branch cannot be
    observed is ``unavailable``, never clean.
    """
    from functools import partial

    from src.integration.delivery_observer import DeliveryTarget
    from src.integration.git_truth import GitTruth, GitTruthSnapshot
    from src.integration.train_sources import project_delivered

    now = time.time() if now is None else now
    query = (
        select(projects.c.id, repos.c.id.label("repository_id"), repos.c.url,
               repos.c.default_branch)
        .select_from(projects.join(repos, repos.c.id == projects.c.integration_repository_id))
        .where(projects.c.hierarchical_integration_mode.in_(DELIVERING_MODES))
        .order_by(projects.c.id)
    )
    if project_ids is not None:
        query = query.where(projects.c.id.in_(sorted(project_ids)))
    async with db._engine.connect() as conn:
        targets = (await conn.execute(query)).mappings().all()
    entries: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for target in targets:
        project_id, repository_id = target["id"], target["repository_id"]
        target_ref = "refs/heads/" + (target["default_branch"] or "").removeprefix("refs/heads/")
        try:
            if not target["url"] or not target["default_branch"]:
                raise ValueError("integration repository has no URL or default branch")
            snapshot = await observer.snapshot(
                DeliveryTarget(project_id, repository_id, target["url"], target_ref),
                max_age=observer.READ_MAX_AGE, cached_only=cached_only,
            )
            observation = getattr(snapshot, "observation", snapshot)
            if observation.error or not observation.target_oid:
                raise ValueError(observation.error or "default branch unavailable")
            truth = (snapshot if isinstance(snapshot, GitTruthSnapshot)
                     else GitTruthSnapshot(GitTruth(observer.git), snapshot,
                                           cached_only=cached_only))
            heads = {
                ref.removeprefix("refs/remotes/origin/"): oid
                for ref, oid in observation.source_heads.items()
                if ref.startswith("refs/remotes/origin/")
            }
            store, tip, git = observation.store, observation.target_oid, observer.git

            async def reachable(head, store=store, tip=tip, git=git):
                return await git.ais_ancestor(store, head, tip, strict=True)

            async def contains(head, branch, store=store, heads=heads, git=git):
                return bool(await git.ais_ancestor(store, head, heads[branch], strict=True))

            async with db._engine.connect() as conn:
                entries.extend(await completed_branch_dispositions(
                    conn, heads=heads, reachable=reachable, contains=contains,
                    delivered=partial(project_delivered, db, project_id=project_id,
                                      repository_id=repository_id, target_ref=target_ref,
                                      snapshot=truth),
                    now=now, project_id=project_id, repository_id=repository_id,
                    task_ids=task_ids,
                ))
        except (GitError, OSError, ValueError) as exc:
            unavailable.append({"project_id": project_id, "repository_id": repository_id,
                                "reason": str(exc)})
    return {"entries": entries, "unavailable": unavailable, "projects": len(targets)}


#: Rules under which something on record explains the branch or will move it.
_ACCOUNTED_RULES = frozenset(
    {"retiring", "carried", "recorded", "delivered", "integrated", "awaiting_delivery",
     "awaiting_backstop"}
)


def _remedy(task_id: str, project_id: str, archived: bool, rule: str) -> str | None:
    """The operator action that settles a finding; ``None`` for anything else."""
    if rule == "retirement_conflict":
        return ("the branch moved after its retirement was requested; a person decides "
                "whether the new commits are wanted before deleting it by hand")
    if rule not in {"stranded_root", "unreconciled"}:
        return None
    if archived:
        return ("the daily Git sweep queues an archived task's retirement; the daemon log "
                "names the failure (`retirement recovery failed`)")
    return (f"aq task archive {task_id} --disposition deliver|obsolete|retire "
            f"--reason \"...\", or, when its work landed under another commit, "
            f"scripts/backfill-legacy-deliveries.py {project_id} "
            f"--relanded {task_id}=<landing-sha> --reason \"...\" --apply")


def _disposition(row, archived, branch, head, wip, rule, reason) -> dict[str, Any]:
    return {
        "task_id": row["id"],
        "project_id": row["project_id"],
        "archived": archived,
        "branch": branch,
        "head": head,
        "wip": wip,
        "rule": rule,
        "accounted": rule in _ACCOUNTED_RULES,
        "reason": reason,
        "remedy": _remedy(row["id"], row["project_id"], archived, rule),
    }


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
    protected: Iterable[str] = (),
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
    ``aq/``, the default branch, promotion targets and ``gh-pages`` are only counted.
    """
    released = released or {}
    expired = expired or {}
    protected = frozenset(protected)
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
        if not deletable(branch, default_branch, protected=protected):
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
    "DELIVERING_MODES",
    "FAILED_BRANCH_KEEP_SECONDS",
    "INTEGRATION_PREFIX",
    "PROTECTED_BRANCHES",
    "ROOT_DELIVERY_WAIT_SECONDS",
    "TASK_BRANCH_PREFIX",
    "branch_of",
    "completed_branch_dispositions",
    "completed_branch_tasks",
    "deletable",
    "delete_branches",
    "expired_task_branches",
    "find_stale_branches",
    "live_branch_references",
    "observe_completed_branches",
    "protected_branches",
    "released_integration_refs",
    "remote_heads",
    "repository_protected_branches",
]
