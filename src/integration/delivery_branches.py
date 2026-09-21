"""Remote branches delivery leaves behind, and which of them may go.

Every worker task pushes ``aq/<task-id>``; every development batch pushes a
candidate snapshot (``aq/development/<project>/<head>``) and, for children,
parent assemblies (``aq/development/parent/<parent>/<assembly>``); every
development repair pushes its own task branch (``aq/development-repair-<id>``,
sometimes with a ``-wip`` sibling).  The publisher lands *merges*, but GitHub
lists every one of those refs as a live branch until somebody deletes it, and
on 2026-09-21 origin carried 961 of them, 893 already merged.

Two callers delete them, and both ask this module the same question first —
"does anything still need this branch?" (:func:`live_branch_references`):

* :meth:`src.integration.development.DevelopmentIntegration.collect_delivered_branches`
  after a batch is confirmed on the default branch, for exactly the refs that
  batch's journal names; and
* ``aq doctor --check integration.landed_branches [--fix]`` for the backlog:
  any ``aq/`` branch whose work is already on the default branch
  (:func:`find_landed_branches`).

Deletion is always a lease on the head that was observed
(:func:`delete_remote_branches`), so a branch somebody pushed to in the
meantime is never removed, and a ref that is already gone is success.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from sqlalchemy import or_, select

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
    task_metadata,
    tasks,
)
from src.git.manager import GitError
from src.integration.live_operations import ACTIVE_OPERATION_STATES
from src.models import TaskStatus

#: Only branches in the daemon's own namespace are ever deleted.  A person's
#: ``fix/...`` branch is theirs to tidy, whatever its ancestry says.
TASK_BRANCH_PREFIX = "aq/"
#: Publisher assemblies: candidate snapshots and parent aggregates.
ASSEMBLY_PREFIX = "aq/development/"

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
PUSH_CHUNK = 50


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


async def delete_remote_branches(git, run_git, store, targets: dict[str, str]) -> dict[str, str]:
    """Delete each ``branch -> expected head`` in *targets* from ``origin``.

    Every delete carries ``--force-with-lease=<ref>:<expected>``, so a branch
    that moved since it was observed is left alone.  The push result is not
    trusted on its own: the remote is listed afterwards and each branch is
    classified from what is actually there —

    * ``deleted`` — gone (including already gone before the push);
    * ``moved``   — present at a different head: someone pushed, keep it;
    * ``failed``  — still at the expected head: transport trouble, retry.

    A failure to *list* the remote raises, which the caller treats as one
    retryable attempt for everything it asked for.
    """
    if not targets:
        return {}
    ordered = sorted(targets)
    for start in range(0, len(ordered), PUSH_CHUNK):
        chunk = ordered[start : start + PUSH_CHUNK]
        args = ["push", "--porcelain", "--no-verify", "origin"]
        args += [f"--force-with-lease=refs/heads/{b}:{targets[b]}" for b in chunk]
        args += [f":refs/heads/{b}" for b in chunk]
        # A rejected lease makes the whole push exit non-zero; the listing
        # below is what decides each branch.
        await git.arun_git_result(args, cwd=str(store))
    listed = await run_git(store, "ls-remote", "--heads", "origin")
    remote = {}
    for line in listed.splitlines():
        sha, _, ref = line.partition("\t")
        remote[ref.removeprefix("refs/heads/")] = sha
    outcomes = {}
    for branch in ordered:
        current = remote.get(branch)
        if current is None:
            outcomes[branch] = "deleted"
        elif current == targets[branch]:
            outcomes[branch] = "failed"
        else:
            outcomes[branch] = "moved"
    return outcomes


# -- the backlog ---------------------------------------------------------


_FIELD = "\x1f"


async def find_landed_branches(
    run_git, store, *, default_branch: str, holds: dict[str, str]
) -> dict[str, Any]:
    """Classify every ``aq/`` branch of ``origin`` against the default branch.

    A branch's work is *on main* when its head is an ancestor of the default
    branch (``ancestry``), or when every non-merge commit it has beyond the
    default branch has a twin there with the same author e-mail, author time
    and subject (``subject``) — how a rebased or cherry-picked copy looks
    (both keep the author stamp; a same-subject commit with another stamp is
    different work).  Either way every merge commit beyond the default branch
    must be *clean*: its tree is exactly what Git's own merge of its parents
    produces.  A hand-resolved ("evil") merge can hold work nothing else
    has, so it keeps the branch.  A branch whose only commits beyond main
    are clean merges of work already on main is ``ancestry`` too.

    Returns ``{"landed": [...], "kept": [...], "unlanded": int,
    "out_of_scope": int, "main_head": sha}``; *landed* entries carry the head
    to lease the delete on and how the work was found (``ancestry`` /
    ``subject``).  Branches in *holds* are reported under *kept* with the
    hold's reason even when their work is on main.
    """
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
    landed, kept = [], []
    unlanded = out_of_scope = 0
    for branch, head in sorted(heads.items()):
        if branch == default_branch:
            continue
        if not branch.startswith(TASK_BRANCH_PREFIX):
            out_of_scope += 1
            continue
        how = None
        if f"refs/remotes/origin/{branch}" in merged:
            how = "ancestry"
        else:
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
                how = "subject" if stamps else "ancestry"
        if how is None:
            unlanded += 1
            continue
        if branch in holds:
            kept.append({"branch": branch, "head": head, "found_by": how,
                         "reason": holds[branch]})
            continue
        landed.append({"branch": branch, "head": head, "found_by": how})
    return {
        "landed": landed,
        "kept": kept,
        "unlanded": unlanded,
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
    "TASK_BRANCH_PREFIX",
    "branch_of",
    "delete_remote_branches",
    "find_landed_branches",
    "live_branch_references",
    "remote_heads",
]
