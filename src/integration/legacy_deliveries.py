"""Adopt the delivered children of parents the train will never collect.

A parent completes under the train only after every child carries a delivery
receipt bound to the parent's current collection
(:meth:`~src.integration.parent_completion.ParentCompletion.readiness_on`).
A parent that finished before the project entered train mode -- delivered by
the development publisher, or before trains existed -- has no collection and
never will, so :class:`~src.integration.status.IntegrationStatusService`
reported each of its terminal children as ``missing_receipt`` and observe
readiness could never pass.

Status accepts such a child (terminal child, terminal parent, no current
collection) on either of two durable facts, read by
:func:`legacy_delivered_children_on`:

* the development publisher's own receipt: a ``delivered`` or ``adopted``
  development delivery to the designated repository's default branch that
  lists the child's latest completion
  (:func:`~src.database.queries.blocked_state.development_delivery_receipt`,
  the same binding blocked-state readiness uses).  Every development-mode
  delivery therefore already writes a receipt train readiness accepts; no
  second copy is kept that could drift from it;
* an ``integration_legacy_deliveries`` row.

``aq integration adopt-legacy-deliveries`` (:class:`LegacyDeliveryAdoption`)
writes those rows for the children status still flags.  After one fetch of
the designated repository it proves each against the current default-branch
tip:

* ``development_delivery`` -- a ``delivered`` or ``adopted`` development
  delivery to any ref lists the child, and the child's source commit or the
  delivery's published commit is an ancestor of the tip (a child delivered
  into a development parent collection that later reached the default
  branch);
* ``branch_tip`` -- the child's branch tip on origin is an ancestor of the
  tip;
* ``content_equivalent`` -- the work reached the default branch under other
  commits (cherry-picked, squashed or re-delivered): merging a development
  delivery's commit, the branch tip or the latest completion commit into the
  tip changes nothing (``git merge-tree --write-tree``).

Anything it cannot prove is listed with its cause and left alone: a child of
a parent that is still open (its completion still needs bound train receipts),
a child that did not complete, or one whose work is not on the default branch.
For the last it also reports what merging the child's work would still change
(``undelivered``), so a human can decide between three explicit, reasoned
decisions for a named terminal child of a terminal parent, never applied in
bulk:

* ``--supersede TASK_ID --by SHA`` -- the work was re-delivered as *SHA*,
  which must be on the default branch (``superseded``);
* ``--retire TASK_ID`` -- the work was abandoned; nothing is deleted
  (``abandoned``);
* ``--accept TASK_ID`` -- accepted without proof (``operator_accepted``).

A dry run fetches but writes nothing.  Rows are keyed by task id and inserted
with ``ON CONFLICT DO NOTHING``, and an adopted child is no longer flagged, so
the command is idempotent.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any, NamedTuple

from sqlalchemy import and_, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.queries.blocked_state import development_delivery_receipt
from src.database.tables import (
    development_deliveries,
    integration_legacy_deliveries,
    projects,
    repos,
    task_completion_records,
    tasks,
)
from src.git.manager import GitError, is_valid_git_oid

logger = logging.getLogger(__name__)

TERMINAL_TASK_STATES = ("COMPLETED", "FAILED", "CANCELLED")

#: How an adopted child's delivery was proven.
DEVELOPMENT_DELIVERY = "development_delivery"
BRANCH_TIP = "branch_tip"
CONTENT_EQUIVALENT = "content_equivalent"
#: Operator decisions for a child no proof reaches.
SUPERSEDED = "superseded"
OPERATOR_ACCEPTED = "operator_accepted"
ABANDONED = "abandoned"

#: How many undelivered paths a listing names before it only counts them.
UNDELIVERED_FILE_LIMIT = 20

#: Per-child outcomes.
ADOPTED = "adopted"
UNPROVEN = "unproven"

#: Why a flagged child was not adopted.
PARENT_NOT_TERMINAL = "parent_not_terminal"
CHILD_NOT_COMPLETED = "child_not_completed"
NOT_ON_DEFAULT_BRANCH = "not_on_default_branch"
STATE_CHANGED = "state_changed"

#: The ``cause`` of the status blocker this command resolves.
NO_PARENT_COLLECTION = "no_parent_collection"

OUTCOMES = ("adopted", "nothing_to_adopt", "blocked", "invalid", "not_found")

_ABBREVIATED_OID = re.compile(r"[0-9a-f]{7,40}")


class _Target(NamedTuple):
    """The default-branch tip every proof in one run is made against."""

    sha: str
    tree: str


async def legacy_delivered_children_on(
    conn, parent: Mapping[str, Any], child_ids: Iterable[str]
) -> set[str]:
    """The terminal children of a terminal *parent* status accepts as delivered.

    Only meaningful for a parent with no current collection: a collected
    parent's readiness belongs to
    :class:`~src.integration.parent_completion.ParentCompletion` and needs
    bound train receipts.  An open parent accepts nothing here either -- its
    completion will still need them.
    """
    ids = sorted(set(child_ids))
    if parent["status"] not in TERMINAL_TASK_STATES or not ids:
        return set()
    child = tasks.alias("legacy_child")
    project = projects.alias("legacy_project")
    repo = repos.alias("legacy_repo")
    recorded = (
        select(literal(1))
        .where(integration_legacy_deliveries.c.task_id == child.c.id)
        .correlate(child)
        .exists()
    )
    published = (
        select(literal(1))
        .select_from(project.join(repo, repo.c.id == project.c.integration_repository_id))
        .where(
            project.c.id == child.c.project_id,
            development_delivery_receipt(child, project, repo),
        )
        .correlate(child)
        .exists()
    )
    rows = await conn.execute(
        select(child.c.id).where(
            child.c.id.in_(ids),
            child.c.status.in_(TERMINAL_TASK_STATES),
            or_(recorded, and_(child.c.status == "COMPLETED", published)),
        )
    )
    return set(rows.scalars().all())


class LegacyDeliveryAdoption:
    """Record legacy delivery rows for the children status flags without a collection."""

    def __init__(
        self,
        db,
        *,
        development,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from src.integration.status import IntegrationStatusService

        self.db = db
        self.development = development
        self.git = development.git
        self.status = IntegrationStatusService(db, clock=clock)
        self.clock = clock

    async def run(
        self,
        project_id: str,
        *,
        principal: str,
        dry_run: bool = False,
        accept: Iterable[str] = (),
        retire: Iterable[str] = (),
        supersede: Mapping[str, str] | None = None,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Prove what can be proven; apply the named decisions to the rest.

        *accept*, *retire* and *supersede* (task id -> re-delivering commit)
        each name terminal children no proof reaches; every one needs
        *reason*.  A named child that turns out provable is adopted by its
        proof and the decision is ignored.
        """
        superseded = dict(supersede or {})
        decisions: dict[str, list[str]] = {}
        for flag, task_ids in (
            ("--accept", accept),
            ("--retire", retire),
            ("--supersede", superseded),
        ):
            for task_id in set(task_ids):
                decisions.setdefault(task_id, []).append(flag)
        project = await self.db.get_project(project_id)
        if project is None:
            return _result("not_found", project_id, dry_run, error="project not found")
        if decisions and not (reason or "").strip():
            return _result(
                "invalid",
                project_id,
                dry_run,
                error="a decision (--accept, --retire, --supersede) requires an audit reason",
            )
        conflicting = sorted(task_id for task_id, flags in decisions.items() if len(flags) > 1)
        if conflicting:
            return _result(
                "invalid",
                project_id,
                dry_run,
                error="a task takes one decision: "
                + ", ".join(f"{t} ({' and '.join(decisions[t])})" for t in conflicting),
            )
        repo = (
            await self.db.get_repo(project.integration_repository_id)
            if project.integration_repository_id
            else None
        )
        if repo is None:
            return _result(
                "invalid",
                project_id,
                dry_run,
                error="project has no designated integration repository",
            )

        flagged = await self._flagged_children(project_id)
        unknown = sorted(task_id for task_id in decisions if task_id not in flagged)
        if unknown:
            return _result(
                "invalid",
                project_id,
                dry_run,
                error=(
                    "the decision names tasks integration status does not report as a legacy "
                    f"missing receipt: {', '.join(unknown)}"
                ),
            )
        if not flagged:
            return _result("nothing_to_adopt", project_id, dry_run)

        from src.integration.development import DevelopmentBusy

        target_ref = "refs/heads/" + repo.default_branch.removeprefix("refs/heads/")
        try:
            async with self.development.exclusion(repo.id):
                store = await self.development.store(repo)
                target_sha = await self.development.remote(store, target_ref)
                if not target_sha:
                    return _result(
                        "blocked", project_id, dry_run, error=f"{target_ref} is absent on origin"
                    )
                replacements: dict[str, str] = {}
                for task_id, sha in sorted(superseded.items()):
                    commit = await self._commit(store, sha)
                    if commit is None or not await self._on_target(store, commit, target_sha):
                        return _result(
                            "invalid",
                            project_id,
                            dry_run,
                            error=(
                                f"--supersede {task_id}: {sha} is not a commit on {target_ref} "
                                f"at {target_sha}"
                            ),
                        )
                    replacements[task_id] = commit
                deliveries = await self._deliveries_by_task(project_id, set(flagged))
                completions = await self._completion_commits(set(flagged))
                target = _Target(target_sha, await self._tree(store, target_sha))
                results = [
                    await self._prove(
                        store,
                        target,
                        repo.id,
                        flagged[task_id],
                        deliveries.get(task_id, []),
                        completions.get(task_id),
                    )
                    for task_id in sorted(flagged)
                ]
        except DevelopmentBusy as exc:
            return _result("blocked", project_id, dry_run, error=str(exc))
        except (GitError, ValueError) as exc:
            return _result(
                "blocked", project_id, dry_run, error=f"origin could not be inspected: {exc}"
            )

        for item in results:
            flags = decisions.get(item["task_id"])
            if item["outcome"] != UNPROVEN or not flags:
                continue
            if item["cause"] == PARENT_NOT_TERMINAL:
                return _result(
                    "invalid",
                    project_id,
                    dry_run,
                    error=(
                        f"{item['task_id']}: its parent is still open; nothing to decide "
                        f"({flags[0]})"
                    ),
                )
            proof = {
                "--accept": OPERATOR_ACCEPTED,
                "--retire": ABANDONED,
                "--supersede": SUPERSEDED,
            }[flags[0]]
            item.update(
                outcome=ADOPTED,
                proof=proof,
                delivered_sha=replacements.get(item["task_id"]),
                development_delivery_id=None,
                unproven_cause=item.pop("cause"),
            )
        return await self._record(
            project_id,
            repository_id=repo.id,
            target_ref=target_ref,
            target_sha=target_sha,
            results=results,
            principal=principal,
            reason=reason,
            dry_run=dry_run,
        )

    async def _flagged_children(self, project_id: str) -> dict[str, dict[str, Any]]:
        """Children status reports as ``missing_receipt`` with no parent collection."""
        child = tasks.alias("legacy_flagged_child")
        async with self.db._engine.connect() as conn:
            parent_ids = (
                await conn.execute(
                    select(tasks.c.id)
                    .where(
                        tasks.c.project_id == project_id,
                        select(literal(1))
                        .where(child.c.parent_task_id == tasks.c.id)
                        .correlate(tasks)
                        .exists(),
                    )
                    .order_by(tasks.c.id)
                )
            ).scalars().all()
        flagged: dict[str, dict[str, Any]] = {}
        for parent_id in parent_ids:
            projection = await self.status.task_blockers(parent_id)
            for blocker in (projection or {}).get("blockers", ()):
                if (
                    blocker["code"] == "missing_receipt"
                    and blocker.get("cause") == NO_PARENT_COLLECTION
                ):
                    flagged[blocker["ref"]] = {
                        "task_id": blocker["ref"],
                        "parent_task_id": parent_id,
                    }
        if not flagged:
            return flagged
        ids = set(flagged) | {item["parent_task_id"] for item in flagged.values()}
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(tasks.c.id, tasks.c.status, tasks.c.branch_name).where(
                        tasks.c.id.in_(sorted(ids))
                    )
                )
            ).mappings().all()
        state = {row["id"]: row for row in rows}
        for task_id, item in flagged.items():
            item["status"] = state[task_id]["status"]
            item["branch_name"] = state[task_id]["branch_name"]
            item["parent_status"] = state[item["parent_task_id"]]["status"]
        return flagged

    async def _deliveries_by_task(
        self, project_id: str, task_ids: set[str]
    ) -> dict[str, list[dict[str, Any]]]:
        """Delivered or adopted development deliveries of the project, by listed task."""
        async with self.db._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(development_deliveries)
                    .where(
                        development_deliveries.c.project_id == project_id,
                        development_deliveries.c.state.in_(("delivered", "adopted")),
                    )
                    .order_by(
                        development_deliveries.c.created_at, development_deliveries.c.id
                    )
                )
            ).mappings().all()
        by_task: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            for member in row["manifest"] or ():
                task_id = member.get("task_id") if isinstance(member, dict) else None
                if task_id in task_ids:
                    by_task.setdefault(task_id, []).append(
                        {
                            "id": row["id"],
                            "repository_id": row["repository_id"],
                            "prepared_sha": row["prepared_sha"],
                            "source_sha": member.get("source_sha"),
                        }
                    )
        return by_task

    async def _completion_commits(self, task_ids: set[str]) -> dict[str, str]:
        """The last commit each task's latest completion reported, by task."""
        latest = (
            select(
                task_completion_records.c.task_id,
                task_completion_records.c.commits,
            )
            .where(task_completion_records.c.task_id.in_(sorted(task_ids)))
            .order_by(
                task_completion_records.c.task_id,
                task_completion_records.c.completed_at.desc(),
                task_completion_records.c.id.desc(),
            )
            .distinct(task_completion_records.c.task_id)
        )
        async with self.db._engine.connect() as conn:
            rows = (await conn.execute(latest)).all()
        commits: dict[str, str] = {}
        for task_id, value in rows:
            try:
                reported = json.loads(value or "[]")
            except ValueError:
                continue
            if isinstance(reported, list) and reported and isinstance(reported[-1], str):
                commits[task_id] = reported[-1]
        return commits

    async def _prove(
        self,
        store,
        target: _Target,
        repository_id: str,
        child: dict[str, Any],
        deliveries: list[dict[str, Any]],
        completion_sha: str | None = None,
    ) -> dict[str, Any]:
        """Adopt *child* on the first proof that reaches *target*; else say why not."""
        item = {"task_id": child["task_id"], "parent_task_id": child["parent_task_id"]}
        if child["parent_status"] not in TERMINAL_TASK_STATES:
            return {
                **item,
                "outcome": UNPROVEN,
                "cause": PARENT_NOT_TERMINAL,
                "detail": (
                    f"parent is {child['parent_status']}: its completion still needs "
                    "current train receipts"
                ),
            }
        if child["status"] != "COMPLETED":
            return {
                **item,
                "outcome": UNPROVEN,
                "cause": CHILD_NOT_COMPLETED,
                "detail": f"child is {child['status']}: no delivered commit to prove",
            }
        delivered: dict[str, str] = {}
        for delivery in deliveries:
            if delivery["repository_id"] != repository_id:
                continue
            for sha in (delivery["source_sha"], delivery["prepared_sha"]):
                if not sha:
                    continue
                delivered.setdefault(sha, delivery["id"])
                if await self._on_target(store, sha, target.sha):
                    return {
                        **item,
                        "outcome": ADOPTED,
                        "proof": DEVELOPMENT_DELIVERY,
                        "delivered_sha": sha,
                        "development_delivery_id": delivery["id"],
                    }
        tip = None
        branch = (child["branch_name"] or "").removeprefix("refs/heads/")
        if branch:
            try:
                tip = await self.development.remote(store, "refs/heads/" + branch)
            except (GitError, ValueError):
                tip = None
            if tip and await self._on_target(store, tip, target.sha):
                return {
                    **item,
                    "outcome": ADOPTED,
                    "proof": BRANCH_TIP,
                    "delivered_sha": tip,
                    "development_delivery_id": None,
                }
        # The work may have reached the default branch under other commits
        # (cherry-picked, squashed, re-delivered): then merging it changes
        # nothing.  The first examinable candidate also shows what is left.
        candidates = list(dict.fromkeys(c for c in (tip, completion_sha, *delivered) if c))
        undelivered = None
        for sha in candidates:
            merge = await self._merge(store, target, sha)
            if merge is None:
                continue
            if merge.get("tree") == target.tree:
                return {
                    **item,
                    "outcome": ADOPTED,
                    "proof": CONTENT_EQUIVALENT,
                    "delivered_sha": merge["sha"],
                    "development_delivery_id": delivered.get(sha),
                }
            if undelivered is None:
                undelivered = await self._undelivered(store, target, merge)
        examined = [f"{len(deliveries)} development deliveries"]
        examined.append(f"branch {branch} at {tip}" if tip else f"branch {branch or '-'} absent")
        examined.append(
            f"completion commit {completion_sha}" if completion_sha else "no completion commit"
        )
        return {
            **item,
            "outcome": UNPROVEN,
            "cause": NOT_ON_DEFAULT_BRANCH,
            "detail": "no delivered commit is on the default branch (examined "
            + "; ".join(examined)
            + ")",
            "undelivered": undelivered,
        }

    async def _on_target(self, store, sha: str, target_sha: str) -> bool:
        return bool(await self.git.ais_ancestor(str(store), sha, target_sha))

    async def _git(self, store, *args: str):
        return await self.git.arun_git_result(list(args), cwd=str(store))

    async def _commit(self, store, sha: str) -> str | None:
        """*sha* resolved to one commit present in *store*, or ``None``."""
        if not _ABBREVIATED_OID.fullmatch(sha or ""):
            return None
        result = await self._git(store, "rev-parse", "--verify", "--quiet", f"{sha}^{{commit}}")
        resolved = result.stdout.strip()
        return resolved if result.returncode == 0 and is_valid_git_oid(resolved) else None

    async def _tree(self, store, sha: str) -> str:
        result = await self._git(store, "rev-parse", "--verify", f"{sha}^{{tree}}")
        if result.returncode:
            raise GitError(result.stderr.strip() or f"{sha} has no tree")
        return result.stdout.strip()

    async def _merge(self, store, target: _Target, sha: str) -> dict[str, Any] | None:
        """Merge *sha* into *target* without touching a ref; ``None`` if unexaminable.

        Needs ``git merge-tree --write-tree`` (Git 2.38+).  A commit absent
        from *store* (its branch deleted and never fetched) cannot be examined.
        """
        commit = await self._commit(store, sha)
        if commit is None:
            return None
        result = await self._git(store, "merge-tree", "--write-tree", target.sha, commit)
        if result.returncode == 1:
            return {"sha": commit, "tree": None, "conflict": True}
        tree = (result.stdout.splitlines() or [""])[0].strip()
        if result.returncode or not is_valid_git_oid(tree):
            return None
        return {"sha": commit, "tree": tree, "conflict": False}

    async def _undelivered(self, store, target: _Target, merge: dict[str, Any]) -> dict[str, Any]:
        """What merging *merge*'s commit into the default branch would still change."""
        if merge["conflict"]:
            return {
                "sha": merge["sha"],
                "conflict": True,
                "summary": "merging it into the default branch conflicts",
            }
        stat = await self._git(store, "diff", "--shortstat", target.tree, merge["tree"])
        names = await self._git(store, "diff", "--name-only", target.tree, merge["tree"])
        files = [line for line in names.stdout.splitlines() if line]
        return {
            "sha": merge["sha"],
            "conflict": False,
            "summary": stat.stdout.strip(),
            "file_count": len(files),
            "files": files[:UNDELIVERED_FILE_LIMIT],
        }

    async def _record(
        self,
        project_id: str,
        *,
        repository_id: str,
        target_ref: str,
        target_sha: str,
        results: list[dict[str, Any]],
        principal: str,
        reason: str | None,
        dry_run: bool,
    ) -> dict[str, Any]:
        adopted = [item for item in results if item["outcome"] == ADOPTED]
        if adopted and not dry_run:
            now = self.clock()
            async with self.db.immediate() as conn:
                await self.db.lock_hierarchy_project(conn, project_id)
                ids = {item["task_id"] for item in adopted} | {
                    item["parent_task_id"] for item in adopted
                }
                status = dict(
                    (
                        await conn.execute(
                            select(tasks.c.id, tasks.c.status).where(
                                tasks.c.id.in_(sorted(ids))
                            )
                        )
                    ).all()
                )
                rows = []
                for item in adopted:
                    # A reopened child or parent is live work again: the train
                    # will need its receipts, so it must not be adopted.
                    if (
                        status.get(item["task_id"]) not in TERMINAL_TASK_STATES
                        or status.get(item["parent_task_id"]) not in TERMINAL_TASK_STATES
                    ):
                        item.update(
                            outcome=UNPROVEN,
                            cause=STATE_CHANGED,
                            detail="child or parent is no longer terminal",
                        )
                        continue
                    rows.append(
                        {
                            "task_id": item["task_id"],
                            "project_id": project_id,
                            "parent_task_id": item["parent_task_id"],
                            "repository_id": repository_id,
                            "target_ref": target_ref,
                            "target_sha": target_sha,
                            "delivered_sha": item["delivered_sha"],
                            "proof": item["proof"],
                            "development_delivery_id": item["development_delivery_id"],
                            "operator_id": principal,
                            "reason": (reason or "").strip()
                            or f"legacy delivery proven by {item['proof']}",
                            "created_at": now,
                        }
                    )
                if rows:
                    await conn.execute(
                        pg_insert(integration_legacy_deliveries)
                        .values(rows)
                        .on_conflict_do_nothing(index_elements=["task_id"])
                    )
            adopted = [item for item in results if item["outcome"] == ADOPTED]
            logger.info(
                "integration.legacy_deliveries_adopted project=%s count=%d principal=%s",
                project_id,
                len(adopted),
                principal,
            )
        return _result(
            "adopted" if adopted else "nothing_to_adopt",
            project_id,
            dry_run,
            head_sha=target_sha,
            count=len(adopted),
            outcomes=results,
        )


def _result(outcome: str, project_id: str, dry_run: bool, **values: Any) -> dict[str, Any]:
    return {
        "outcome": outcome,
        "project_id": project_id,
        "dry_run": dry_run,
        **({"count": 0, "outcomes": []} | values),
    }


def legacy_delivery_adoption_for(handler: Any) -> LegacyDeliveryAdoption | None:
    """The command handler's adoption service, or ``None`` without a DB and Git."""
    db = getattr(handler, "db", None)
    if db is None or getattr(getattr(handler, "orchestrator", None), "git", None) is None:
        return None
    return LegacyDeliveryAdoption(db, development=handler._development_integration())
