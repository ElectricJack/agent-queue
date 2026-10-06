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
   nothing (``git merge-tree --write-tree``) proves the work arrived under
   other commits (``content_equivalent``).

Anything unproven is listed with what was tried and left alone. A dry run
fetches but writes nothing; apply rechecks that the task and its completion
did not change since the proof and inserts with ``ON CONFLICT DO NOTHING``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.database.tables import (
    integration_batch_members,
    integration_legacy_deliveries,
    repos,
    task_completion_records,
    task_delivery_receipts,
    tasks,
)
from src.git.manager import GitError, is_valid_git_oid
from src.integration.delivery_truth import load_delivery_requests
from src.integration.provenance import CompletionIdentity, GitProvenance
from src.integration.train import TrainTarget
from src.integration.train_sources import _pending_tasks, project_delivered, project_snapshot

CONTAINED_PROOF = "development_delivery"
EQUIVALENT_PROOF = "content_equivalent"
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
        rc, tree = await _git(observation, "merge-tree", "--write-tree", target_oid, sha)
        if rc == 0 and tree.splitlines()[:1] == [target_tree]:
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
    requests = await load_delivery_requests(
        db, [task_id for task_id in ids if task_id not in delivered],
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
            async with db._engine.connect() as conn:
                candidates = await _candidates(conn, task_id, request.branch_name,
                                               observation.source_heads)
            await _prove(observation, snap.target_oid, target_tree, verdict, candidates)
            if verdict.outcome == "proven" and not dry_run:
                verdict.outcome = await _record(
                    db, request, target, snap.target_oid, verdict,
                    operator_id=operator_id, reason=reason, now=clock(),
                )
            verdicts.append(verdict)
    finally:
        rc, pulled = await _git(observation, "for-each-ref", "--format=%(refname)",
                                PULL_NAMESPACE)
        for ref in pulled.splitlines() if rc == 0 else ():
            await _git(observation, "update-ref", "-d", ref)
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


async def _record(db, request, target, target_oid, verdict, *, operator_id, reason, now) -> str:
    """Insert one row if the task and completion are still the proven ones."""
    async with db._engine.begin() as conn:
        row = (await conn.execute(
            select(tasks.c.status, tasks.c.updated_at, tasks.c.parent_task_id)
            .where(tasks.c.id == request.task_id).with_for_update()
        )).first()
        latest = await conn.scalar(
            select(task_completion_records.c.id)
            .where(task_completion_records.c.task_id == request.task_id)
            .order_by(task_completion_records.c.completed_at.desc(),
                      task_completion_records.c.id).limit(1)
        )
        if (row is None or row.status != "COMPLETED" or row.updated_at != request.task_version
                or latest != request.completion_id):
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
    return "recorded"


__all__ = ["CONTAINED_PROOF", "EQUIVALENT_PROOF", "backfill_legacy_deliveries"]
