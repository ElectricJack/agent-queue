"""Record git-proven delivery of completions that predate retained provenance.

The git-first train judges a completion only by its retained provenance record
(:mod:`src.integration.provenance`). Work the old engine or a merged pull
request delivered before that record existed stays ``missing_git_provenance``
forever: it is listed as an unknown blocker on the root target, every
dependent that needs it is held out of a batch, and each visit re-evaluates it.

The train already honours one database answer for such work, an
``integration_legacy_deliveries`` row (:func:`src.integration.train_sources.project_delivered`).
This backfill writes that row only after git proves the work is on the root
target tip. It never writes provenance, never moves a ref and never edits a
task. Each completed task with a live origin, no legacy row and no retained
completion record goes through four steps:

1. **Locate** exact candidate sources, newest evidence first: the completion's
   reported commits, the origin branch tip, the old engine's batch member
   sources and delivery receipts, and the head of the task's pull request.
2. **Exist**: a candidate that is not a commit in the fetched store is skipped.
3. **Contain**: a candidate that is an ancestor of the target tip proves
   delivery (``development_delivery``).
4. **Equivalent**: otherwise, a candidate whose merge into the tip changes
   only generated artifacts (``merge=aq-generated`` in the target's attributes)
   proves the work arrived under other commits (``content_equivalent``).

Anything unproven is listed with what was tried and left alone. A dry run
fetches but writes nothing; apply rechecks that the task and its completion
did not change since the proof and inserts with ``ON CONFLICT DO NOTHING``.

Where git can prove nothing and the work must not be delivered now either, an
operator records the decision instead: :func:`abandon_task` for one completed
completion, :func:`abandon_epic` for a container by name. Both write the same
audited ``abandoned`` row, and both refuse work git already proves.

Where the work did land, but under other commits (a salvage, repair or squash
re-landing), :func:`record_relanding` records the operator-named landing commit
as a ``superseded`` row once git shows that commit on the default branch. An
abandoned or re-landed task's branches are then queued for retirement, which
bundles every unmerged tip before deleting it.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    integration_batch_members,
    integration_legacy_deliveries,
    repos,
    task_branch_origins,
    task_completion_records,
    task_delivery_receipts,
    tasks,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.delivery_truth import load_delivery_requests
from src.integration.git_truth import merge_noop
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.train import TrainTarget
from src.integration.train_sources import _pending_tasks, project_delivered, project_snapshot

CONTAINED_PROOF = "development_delivery"
EQUIVALENT_PROOF = "content_equivalent"
#: An operator-named re-landing: the task's content is on the target under the
#: recorded commit (a salvage, repair or squash), not under its own source.
RELANDED_PROOF = "superseded"
#: Decisions that end a source branch's life, by retirement request prefix.
_RETIRING_PROOFS = {"abandoned": "legacy-abandon", RELANDED_PROOF: "legacy-relanded"}
#: A child of a still-open epic whose exact source is on the epic branch: the
#: epic's own readiness reads retained provenance, so that is what is written.
EPIC_PROOF = "epic_branch_provenance"
_PULL = re.compile(r"/pull/(\d+)(?:$|[/?#])")
#: Private namespace for fetched pull-request heads; removed after each run.
PULL_NAMESPACE = "refs/aq-legacy-backfill/pull/"


@dataclass
class Verdict:
    task_id: str
    outcome: str  # proven | unproven | recorded | changed
    proof: str | None = None
    via: str | None = None
    delivered_sha: str | None = None
    tried: list[tuple[str, str]] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "outcome": self.outcome, "proof": self.proof,
                "via": self.via, "delivered_sha": self.delivered_sha,
                "tried": [list(item) for item in self.tried]}


async def _git(observation, *args) -> tuple[int, str]:
    result = await observation.git.arun_git_result(list(args), cwd=str(observation.store))
    return result.returncode, (result.stdout or "").strip()


async def _drop_pull_refs(observation) -> None:
    """Delete every fetched pull-request head: this namespace is private."""
    rc, pulled = await _git(observation, "for-each-ref", "--format=%(refname)", PULL_NAMESPACE)
    for ref in pulled.splitlines() if rc == 0 else ():
        await _git(observation, "update-ref", "-d", ref)


async def _candidates(conn, task_id: str, branch: str | None, refs) -> list[tuple[str, str]]:
    """Exact candidate sources for *task_id*, strongest evidence first."""
    found: list[tuple[str, str]] = []
    completion = (await conn.execute(
        select(task_completion_records.c.commits, task_completion_records.c.pr_url)
        .where(task_completion_records.c.task_id == task_id)
        .order_by(task_completion_records.c.completed_at.desc(), task_completion_records.c.id)
        .limit(1)
    )).first()
    try:
        commits = json.loads(completion[0]) if completion else []
    except (TypeError, ValueError):
        commits = []
    found += [("completion_commit", sha) for sha in reversed(commits) if isinstance(sha, str)]
    if branch and refs.get("refs/remotes/origin/" + branch):
        found.append(("branch_tip", refs["refs/remotes/origin/" + branch]))
    found += [("batch_member", sha) for sha in (await conn.execute(
        select(integration_batch_members.c.source_sha)
        .where(integration_batch_members.c.task_id == task_id)
    )).scalars().all()]
    for row in (await conn.execute(
        select(task_delivery_receipts.c.reviewed_head_sha, task_delivery_receipts.c.squash_sha,
               task_delivery_receipts.c.after_sha)
        .where(task_delivery_receipts.c.source_task_id == task_id)
    )).all():
        found += [("delivery_receipt", sha) for sha in row if sha]
    pr_url = (await conn.scalar(select(tasks.c.pr_url).where(tasks.c.id == task_id))) or (
        completion[1] if completion else None)
    if pr_url and (match := _PULL.search(pr_url)):
        found.append(("pull_request", match.group(1)))
    seen, ordered = set(), []
    for kind, value in found:
        if value and (kind, value) not in seen:
            seen.add((kind, value))
            ordered.append((kind, value))
    return ordered


async def _prove(observation, target_oid: str, target_tree: str, verdict: Verdict,
                 candidates) -> None:
    for kind, value in candidates:
        sha = value
        if kind == "pull_request":
            ref = PULL_NAMESPACE + value
            rc, _ = await _git(observation, "fetch", "--no-tags", "-q", "origin",
                               f"+refs/pull/{value}/head:{ref}")
            rc, sha = (rc, "") if rc else await _git(observation, "rev-parse", "--verify", ref)
            if rc or not sha:
                verdict.tried.append((kind, value + ":unfetchable"))
                continue
        if not is_valid_git_oid(sha):
            verdict.tried.append((kind, f"{sha}:invalid"))
            continue
        verdict.tried.append((kind, sha))
        if (await _git(observation, "cat-file", "-e", sha + "^{commit}"))[0]:
            continue
        if (await _git(observation, "merge-base", "--is-ancestor", sha, target_oid))[0] == 0:
            verdict.outcome, verdict.proof, verdict.via, verdict.delivered_sha = (
                "proven", CONTAINED_PROOF, kind, sha)
            return
        if await merge_noop(observation, sha, target_oid, target_tree=target_tree):
            verdict.outcome, verdict.proof, verdict.via, verdict.delivered_sha = (
                "proven", EQUIVALENT_PROOF, kind, sha)
            return


async def backfill_legacy_deliveries(db, project_id: str, *, dry_run: bool = True,
                                     operator_id: str = "operator", reason: str = "",
                                     snapshot=project_snapshot, clock=time.time) -> dict:
    """Preview or record git-proven deliveries for *project_id*'s root target."""
    if not dry_run and not reason.strip():
        raise ValueError("recording legacy deliveries requires a nonblank reason")
    project = await db.get_project(project_id)
    if project is None or not project.integration_repository_id:
        raise ValueError("project has no integration repository")
    async with db._engine.connect() as conn:
        default = await conn.scalar(select(repos.c.default_branch).where(
            repos.c.id == project.integration_repository_id))
    if not default:
        raise ValueError("integration repository has no default branch")
    target = TrainTarget(project_id, project.integration_repository_id,
                         "refs/heads/" + default.removeprefix("refs/heads/"))
    snap = await snapshot(db, target)
    if snap is None or snap.observation.error or not snap.target_oid:
        raise ValueError("root target cannot be observed")
    observation = snap.observation
    rc, target_tree = await _git(observation, "rev-parse", snap.target_oid + "^{tree}")
    if rc:
        raise ValueError("root target tree cannot be read")
    async with db._engine.connect() as conn:
        ids = await _pending_tasks(conn, project_id, target.repository_id, limit=None)
    delivered = await project_delivered(
        db, ids, project_id=project_id, repository_id=target.repository_id,
        target_ref=target.target_ref, snapshot=snap,
    )
    async with db._engine.connect() as conn:
        # An epic's delivery is its readiness (children collected, checks,
        # review), never a row: its branch sits on the tip before any child is
        # collected. Only an epic whose branch carries collected work (its tip
        # moved past the origin base) can be proven by that tip; otherwise
        # abandoning an old epic is the explicit decision instead.
        parents = set((await conn.execute(select(tasks.c.parent_task_id).where(
            tasks.c.parent_task_id.in_(ids)))).scalars().all())
        for parent_id, branch, base in (await conn.execute(
            select(tasks.c.id, tasks.c.branch_name, task_branch_origins.c.base_sha)
            .select_from(tasks.join(task_branch_origins, and_(
                task_branch_origins.c.task_id == tasks.c.id,
                task_branch_origins.c.retired_at.is_(None))))
            .where(tasks.c.id.in_(parents))
        )).all():
            tip = observation.source_heads.get("refs/remotes/origin/" + (branch or ""))
            if tip and base and tip != base:
                parents.discard(parent_id)
    requests = await load_delivery_requests(
        db, [task_id for task_id in ids if task_id not in delivered and task_id not in parents],
        repository_id=target.repository_id, target_ref=target.target_ref, reduced=True,
    )
    provenance = GitProvenance(observation.git, observation.store,
                               repository_url=observation.repository_url)
    verdicts: list[Verdict] = []
    try:
        for task_id, request in sorted(requests.items()):
            if request.task_status != "COMPLETED" or request.parent_completion is not None:
                continue
            identity = CompletionIdentity(project_id, target.repository_id, task_id,
                                          request.completion_id or request.legacy_generation)
            try:
                if await provenance.read_completion(identity, refs=observation.source_heads):
                    continue  # retained provenance: the train already answers it
            except (GitError, ValueError):
                pass
            verdict = Verdict(task_id, "unproven")
            if not _answered_by_row(request):
                # A committed transition without its completion row: no
                # legacy row can answer that generation, so prove nothing.
                verdict.tried.append(("generation", "uncommitted_completion_row"))
                verdicts.append(verdict)
                continue
            async with db._engine.connect() as conn:
                candidates = await _candidates(conn, task_id, request.branch_name,
                                               observation.source_heads)
            await _prove(observation, snap.target_oid, target_tree, verdict, candidates)
            if verdict.outcome != "proven":
                await _prove_on_open_epic(db, observation, task_id, verdict, candidates)
            if verdict.proof == EPIC_PROOF and not dry_run:
                verdict.outcome = await _attest(db, provenance, request, target, identity,
                                                verdict)
            elif verdict.outcome == "proven" and not dry_run:
                verdict.outcome = await _record(
                    db, request, target, snap.target_oid, verdict,
                    operator_id=operator_id, reason=reason, now=clock(),
                )
            verdicts.append(verdict)
    finally:
        await _drop_pull_refs(observation)
    proven = [v for v in verdicts if v.outcome in {"proven", "recorded"}]
    return {
        "outcome": "preview" if dry_run else "recorded",
        "project_id": project_id,
        "target_ref": target.target_ref,
        "target_sha": snap.target_oid,
        "dry_run": dry_run,
        "examined": len(verdicts),
        "proven": len(proven),
        "recorded": sum(v.outcome == "recorded" for v in verdicts),
        "changed": sum(v.outcome == "changed" for v in verdicts),
        "unproven": [v.as_dict() for v in verdicts if v.outcome == "unproven"],
        "results": [v.as_dict() for v in verdicts if v.outcome != "unproven"],
    }


async def _prove_on_open_epic(db, observation, task_id, verdict, candidates) -> None:
    """Prove an exact candidate is an ancestor of the open parent epic's branch."""
    async with db._engine.connect() as conn:
        parent_id = await conn.scalar(select(tasks.c.parent_task_id).where(tasks.c.id == task_id))
        parent = (await conn.execute(select(tasks.c.status, tasks.c.branch_name)
                                     .where(tasks.c.id == parent_id))).first() if parent_id else None
    if parent is None or parent.status == "COMPLETED" or not parent.branch_name:
        return
    tip = observation.source_heads.get("refs/remotes/origin/" + parent.branch_name)
    if not tip:
        return
    for kind, sha in candidates:
        if kind == "pull_request" or not is_valid_git_oid(sha):
            continue
        if (await _git(observation, "merge-base", "--is-ancestor", sha, tip))[0] == 0:
            verdict.outcome, verdict.proof, verdict.via, verdict.delivered_sha = (
                "proven", EPIC_PROOF, kind, sha)
            return


async def _attest(db, provenance, request, target, identity, verdict) -> str:
    """Retain the exact epic-branch source as the generation's provenance."""
    current = (await load_delivery_requests(
        db, [request.task_id], repository_id=target.repository_id,
        target_ref=target.target_ref, reduced=True,
    )).get(request.task_id)
    if current != request:
        return "changed"
    from src.integration.provenance import CompletedSource

    await provenance.write_completion(CompletedSource(identity, verdict.delivered_sha),
                                      claim_epoch=request.claim_epoch)
    return "recorded"


async def abandon_epic(db, project_id: str, epic_id: str, *, dry_run: bool = True,
                       operator_id: str = "operator", reason: str = "",
                       snapshot=project_snapshot, clock=time.time) -> dict:
    """Preview or record an explicit decision that a completed epic is superseded.

    For an old completed container whose branch never reached the default branch
    and must not now (its work arrived by other means or was replaced), the
    train would otherwise keep the epic target blocked or merge a stale branch.
    This writes an ``abandoned`` row, an audited operator decision rather than a
    proof, for the container and each completed descendant with a live origin
    and no row yet; a container that never had a branch of its own is decided
    the same way. Refused when git already proves the work on the default branch
    (then the ordinary backfill records that proof), when the container is not
    completed, or when it has no children. Nothing is deleted.
    """
    return await _abandon(db, project_id, epic_id, dry_run=dry_run, operator_id=operator_id,
                          reason=reason, snapshot=snapshot, clock=clock, require_children=True)


async def abandon_task(db, project_id: str, task_id: str, *, dry_run: bool = True,
                       operator_id: str = "operator", reason: str = "",
                       snapshot=project_snapshot, clock=time.time) -> dict:
    """Preview or record an explicit decision that one completion is superseded.

    A leaf or branchless completion the train can never prove, and that must not
    now be delivered, stays an unknown blocker on its target for good: no proof
    of it exists, so every dependent stays held out of every batch and each
    visit re-evaluates it. This records the same audited ``abandoned`` row
    :func:`abandon_epic` records for that one completed task, and, when the task
    is a container, for its undelivered descendants. Refused when git already
    proves the work on the default branch or the task is not completed.
    """
    return await _abandon(db, project_id, task_id, dry_run=dry_run, operator_id=operator_id,
                          reason=reason, snapshot=snapshot, clock=clock, require_children=False)


async def _abandon(db, project_id: str, task_id: str, *, dry_run: bool, operator_id: str,
                   reason: str, snapshot, clock, require_children: bool) -> dict:
    """The audited decision behind both abandon surfaces, one named task wide."""
    if not dry_run and not reason.strip():
        raise ValueError("abandoning undelivered work requires a nonblank reason")
    project = await db.get_project(project_id)
    task = await db.get_task(task_id)
    if project is None or task is None or task.project_id != project_id:
        raise ValueError("task is not a task of this project")
    if getattr(task.status, "value", task.status) != "COMPLETED":
        raise ValueError("only a completed task can be abandoned")
    target, snap, target_tree = await _observe_root(db, project, snapshot)
    observation = snap.observation
    branch = task.branch_name
    tip = observation.source_heads.get("refs/remotes/origin/" + branch) if branch else None
    ids, frontier = [task_id], [task_id]
    async with db._engine.connect() as conn:
        while frontier:
            children = list((await conn.execute(select(tasks.c.id).where(
                tasks.c.parent_task_id.in_(frontier)))).scalars().all())
            ids += children
            frontier = children
        if require_children and len(ids) == 1:
            raise ValueError("only a completed epic with children can be abandoned")
        candidates = await _candidates(conn, task_id, branch, observation.source_heads)
        bases = list((await conn.execute(select(task_branch_origins.c.base_sha).where(
            task_branch_origins.c.task_id == task_id,
            task_branch_origins.c.retired_at.is_(None)))).scalars())
        pending = set(await _pending_tasks(conn, project_id, target.repository_id, limit=None))
        recorded = set((await conn.execute(select(integration_legacy_deliveries.c.task_id).where(
            integration_legacy_deliveries.c.task_id.in_(ids)))).scalars().all())
    await _refuse_if_proven(
        db, observation, task_id, candidates, container=len(ids) > 1, tip=tip, bases=bases,
        target_oid=snap.target_oid, target_tree=target_tree,
    )
    delivered = await project_delivered(
        db, ids, project_id=project_id, repository_id=target.repository_id,
        target_ref=target.target_ref, snapshot=snap)
    chosen = sorted(i for i in ids if i in pending and i not in recorded and i not in delivered)
    requests = await load_delivery_requests(db, chosen, repository_id=target.repository_id,
                                            target_ref=target.target_ref, reduced=True)
    results = []
    for chosen_id in chosen:
        request = requests.get(chosen_id)
        verdict = Verdict(chosen_id, "proven", "abandoned", "operator_decision", None)
        if request is None or not _answered_by_row(request):
            verdict.outcome = "unproven"
        elif not dry_run:
            verdict.outcome = await _record(
                db, request, target, snap.target_oid, verdict, operator_id=operator_id,
                reason=f"abandon {task_id}: {reason}", now=clock())
        results.append(verdict.as_dict())
    return {"outcome": "preview" if dry_run else "recorded", "task_id": task_id,
            "branch_tip": tip, "target_sha": snap.target_oid, "dry_run": dry_run,
            "results": results}


async def record_relanding(db, project_id: str, task_id: str, *, landed_sha: str,
                           dry_run: bool = True, operator_id: str = "operator",
                           reason: str = "", snapshot=project_snapshot,
                           clock=time.time) -> dict:
    """Preview or record that a completion's content landed under another commit.

    Salvage, repair and squash re-landings put a task's work on the default
    branch under new SHAs. No ancestry or no-op merge check can then prove the
    task's own branch delivered, so the train keeps it as an unknown blocker and
    its branch looks unmerged forever. The operator names the landing commit,
    which must be on the default branch tip; this records a ``superseded`` row
    linking the task to it and queues the task's branches for retirement, which
    bundles each unmerged tip before deleting it (:mod:`src.integration.branch_retirement`).

    Refused when the task is not completed or belongs to another project, when
    the landing commit is not on the default branch, and when git already proves
    the task's own source there (then the ordinary backfill records that proof).
    A container's undelivered descendants are not decided here: each re-landed
    task is named on its own, or abandoned.
    """
    if not dry_run and not reason.strip():
        raise ValueError("recording a re-landing requires a nonblank reason")
    if not is_valid_git_oid(landed_sha or ""):
        raise ValueError("the landing commit must be a full git object id")
    project = await db.get_project(project_id)
    task = await db.get_task(task_id)
    if project is None or task is None or task.project_id != project_id:
        raise ValueError("task is not a task of this project")
    if getattr(task.status, "value", task.status) != "COMPLETED":
        raise ValueError("only a completed task can be recorded as re-landed")
    target, snap, target_tree = await _observe_root(db, project, snapshot)
    observation = snap.observation
    if (await _git(observation, "cat-file", "-e", landed_sha + "^{commit}"))[0]:
        raise ValueError(f"landing commit {landed_sha[:12]} is not a fetched commit")
    if (await _git(observation, "merge-base", "--is-ancestor", landed_sha,
                   snap.target_oid))[0]:
        raise ValueError(
            f"landing commit {landed_sha[:12]} is not on {target.target_ref} at "
            f"{snap.target_oid[:12]}")
    async with db._engine.connect() as conn:
        candidates = await _candidates(conn, task_id, task.branch_name,
                                       observation.source_heads)
        recorded = await conn.scalar(select(integration_legacy_deliveries.c.proof).where(
            integration_legacy_deliveries.c.task_id == task_id))
    if recorded is not None:
        raise ValueError(f"{task_id} already has a recorded {recorded} delivery")
    await _refuse_if_proven(
        db, observation, task_id, candidates, container=False, tip=None, bases=(),
        target_oid=snap.target_oid, target_tree=target_tree,
    )
    request = (await load_delivery_requests(
        db, [task_id], repository_id=target.repository_id, target_ref=target.target_ref,
        reduced=True,
    )).get(task_id)
    verdict = Verdict(task_id, "proven", RELANDED_PROOF, "operator_relanding", landed_sha)
    if request is None or not _answered_by_row(request):
        verdict.outcome = "unproven"
        verdict.tried.append(("generation", "uncommitted_completion_row"))
    elif not dry_run:
        verdict.outcome = await _record(
            db, request, target, snap.target_oid, verdict, operator_id=operator_id,
            reason=f"relanded {task_id} as {landed_sha}: {reason}", now=clock())
    return {"outcome": "preview" if dry_run else "recorded", "task_id": task_id,
            "landed_sha": landed_sha, "target_sha": snap.target_oid, "dry_run": dry_run,
            "results": [verdict.as_dict()]}


async def _observe_root(db, project, snapshot):
    """The project's root target, its observed snapshot and the tip's tree."""
    if not project.integration_repository_id:
        raise ValueError("project has no integration repository")
    async with db._engine.connect() as conn:
        default = await conn.scalar(select(repos.c.default_branch).where(
            repos.c.id == project.integration_repository_id))
    if not default:
        raise ValueError("integration repository has no default branch")
    target = TrainTarget(project.id, project.integration_repository_id,
                         "refs/heads/" + default.removeprefix("refs/heads/"))
    snap = await snapshot(db, target)
    if snap is None or snap.observation.error or not snap.target_oid:
        raise ValueError("root target cannot be observed")
    rc, target_tree = await _git(snap.observation, "rev-parse", snap.target_oid + "^{tree}")
    if rc:
        raise ValueError("root target tree cannot be read")
    return target, snap, target_tree


async def _refuse_if_proven(db, observation, task_id, candidates, *, container: bool,
                            tip: str | None, bases, target_oid: str, target_tree: str) -> None:
    """Refuse a decision for work git already proves, so no two answers disagree.

    A proof is the better answer than an operator decision, so a decision that
    contradicts one never lands: run the ordinary backfill instead. A container
    whose branch still equals its origin base carries no collected work, which is
    exactly what the row backfill declines to read as delivery, so nothing it
    could record contradicts the decision and the check is skipped.
    """
    if container and not any(tip and base and tip != base for base in bases):
        return
    verdict = Verdict(task_id, "unproven")
    try:
        await _prove(observation, target_oid, target_tree, verdict, candidates)
        if verdict.outcome != "proven" and not container:
            await _prove_on_open_epic(db, observation, task_id, verdict, candidates)
    finally:
        await _drop_pull_refs(observation)
    if verdict.outcome == "proven":
        where = ("its parent epic's branch" if verdict.proof == EPIC_PROOF
                 else f"the default branch at {target_oid[:12]}")
        raise ValueError(
            f"git already proves {task_id} on {where} ({verdict.proof} via "
            f"{verdict.via}); use the ordinary backfill")


def _answered_by_row(request) -> bool:
    """Whether :func:`project_delivered` would honour a row written now."""
    return request.completed_at is not None or (
        request.completion_id == request.legacy_generation)


async def _record(db, request, target, target_oid, verdict, *, operator_id, reason, now) -> str:
    """Insert one row if the task and completion are still the proven ones."""
    async with db._engine.begin() as conn:
        row = (await conn.execute(
            select(tasks.c.parent_task_id).where(tasks.c.id == request.task_id).with_for_update()
        )).first()
        current = (await load_delivery_requests(
            db, [request.task_id], repository_id=target.repository_id,
            target_ref=target.target_ref, conn=conn, reduced=True,
        )).get(request.task_id)
        if row is None or current != request:
            return "changed"
        await conn.execute(pg_insert(integration_legacy_deliveries).values(
            task_id=request.task_id, project_id=target.project_id,
            parent_task_id=row.parent_task_id or "", repository_id=target.repository_id,
            target_ref=target.target_ref, target_sha=target_oid,
            delivered_sha=verdict.delivered_sha, proof=verdict.proof,
            development_delivery_id=None, operator_id=operator_id,
            reason=f"{reason.strip()} [via {verdict.via} {verdict.delivered_sha}]",
            created_at=now,
        ).on_conflict_do_nothing(index_elements=[integration_legacy_deliveries.c.task_id]))
        if verdict.proof in _RETIRING_PROOFS:
            from src.integration.branch_retirement import request_task_retirement_on

            await request_task_retirement_on(
                conn, request.task_id,
                request_id=f"{_RETIRING_PROOFS[verdict.proof]}:{request.task_id}",
                reason=reason, now=now,
            )
    return "recorded"


__all__ = ["CONTAINED_PROOF", "EPIC_PROOF", "EQUIVALENT_PROOF", "RELANDED_PROOF",
           "abandon_epic", "abandon_task", "backfill_legacy_deliveries", "record_relanding"]
