"""integration.* doctor checks — did finished work actually get reviewed?

The outage this guards against is silent by construction.  A worker commits,
pushes, opens a PR and closes ``pass``; the task goes COMPLETED and every
surface says the work is done.  Whether a reviewer was ever spawned is decided
one layer down, by the default pipeline reacting to ``task.completed`` — and
when that reaction stops firing, nothing anywhere reports an error.  Work just
piles up as open PRs nobody looks at.

That is exactly what happened after the session-runtime cutover: the only
``bus.emit("task.completed", ...)`` for an ordinary task lived in the legacy
blocking tail of ``_execute_task``, below the "Session-runtime fork" that every
agent now takes.  Nine PRs sat open before a human noticed.

``integration.unreviewed_prs`` is the alarm that was missing.  It compares two
things doctor can see directly — a recently COMPLETED task that carries a PR,
and the review task the pipeline would have created for it — and warns when the
first exists without the second.

Mirrors ``src/doctor/pool_checks.py``'s shape: a private ``_find_*``/``_check_*``
pair, a factory returning the :class:`DoctorCheck` list, a ``CHECKS`` snapshot
and a ``run_check`` wrapper for tests and ad-hoc calls.
"""

from __future__ import annotations

import json
import time

from src.database.queries.integration_state_queries import session_attached_clause
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.integration.live_operations import (
    cancel_preserving_command,
    describe_live_operation,
    live_operations_on,
)
from src.integration.owner_recovery import owner_recovery_for
from src.models import TaskStatus
from src.review_keys import review_task_dedup_key

OWNER = "integration"

#: How far back to look.  Long enough that a real stall is caught the same
#: working session, short enough that historical debt (PRs deliberately left
#: open, work from before this check existed) does not permanently redden the
#: report.
_WINDOW_SECONDS = 24 * 60 * 60

#: A publisher fault that survives two consecutive ticks is a stall, not a
#: transient: the batch state it reads is durable, so the same tick that failed
#: will keep failing until someone looks.  One tick is allowed to be noise.
_STALL_TICKS = 2
_SKIP_STALL_TICKS = 3

#: A repair branch closes ``pass`` and the very next sweep should collect it.
#: An hour is twelve sweeps at the default five-minute interval — long enough
#: that a busy or briefly-stopped daemon is not reported, short enough that a
#: publisher which has stopped collecting is caught the same working session.
_UNCOLLECTED_AFTER_SECONDS = 60 * 60

#: A completed child is assembled within a few collector ticks of its close
#: (the collector runs every few seconds).  Five minutes covers a busy or
#: briefly restarted daemon; past that its siblings are being held back.
_STUCK_CHILD_AFTER_SECONDS = 5 * 60

#: Cap on ``gh pr view`` calls per run.  Doctor is meant to be fast and to work
#: offline; a backlog of 200 stranded PRs is already diagnosed by the first
#: handful, and the count in ``data`` stays accurate regardless.
_MAX_PR_PROBES = 20


def _operational_projection(status: dict) -> dict:
    """Keep the operator-relevant, non-secret part of ``integration_status``."""
    repair = list(status.get("repair") or [])
    cleanup = list(status.get("cleanup_pending") or [])
    return {
        "project_id": status.get("project_id"),
        "effective_mode": status.get("effective_mode"),
        "desired_mode": status.get("desired_mode"),
        "generation": status.get("generation"),
        "draining": bool(status.get("draining")),
        "ready": bool(status.get("rollout_ready", status.get("ready", False))),
        "repository_id": status.get("repository_id"),
        "blockers": list(status.get("blockers") or []),
        "blocker_digest": status.get("blocker_digest"),
        "certification": status.get("certification"),
        "active_batch": status.get("active_batch"),
        "human_required": [
            {"operation_id": item.get("id"), "state": item.get("state")}
            for item in repair
            if item.get("state") == "human_required"
        ],
        "live_operations": list(status.get("live_operations") or []),
        "cleanup_attention": [
            item
            for item in cleanup
            if item.get("state") in {"conflict", "failed", "human_required"}
            or bool(item.get("irreversible"))
        ],
    }


async def _check_operational(ctx: DoctorContext) -> CheckResult:
    """Aggregate the reviewed read-only status command for every project.

    The command is the authority for functional preflight and operational
    state.  Doctor deliberately does not inspect credentials, run a provider
    probe, retry cleanup, change modes, or repair schema.
    """
    if ctx.db is None or ctx.handler is None:
        return CheckResult(
            id="integration.operational",
            severity=Severity.INFO,
            detail="integration status unavailable without database and command handler",
        )

    try:
        projects = sorted(await ctx.db.list_projects(), key=lambda project: project.id)
    except Exception as exc:
        return CheckResult(
            id="integration.operational",
            severity=Severity.ERROR,
            detail="could not enumerate projects; check db.migrations",
            data={"errors": [{"error": f"{type(exc).__name__}: {exc}"}]},
        )

    projections: list[dict] = []
    errors: list[dict[str, str]] = []
    for project in projects:
        try:
            status = await ctx.handler.execute(
                "integration_status", {"project_id": project.id}
            )
        except Exception as exc:
            errors.append(
                {"project_id": project.id, "error": f"{type(exc).__name__}: {exc}"}
            )
            continue
        if not isinstance(status, dict) or status.get("outcome") != "status":
            errors.append(
                {
                    "project_id": project.id,
                    "error": str(
                        status.get("error") or status.get("outcome") or "invalid status result"
                    )
                    if isinstance(status, dict)
                    else f"invalid status result: {type(status).__name__}",
                }
            )
            continue
        projections.append(_operational_projection(status))

    if errors:
        return CheckResult(
            id="integration.operational",
            severity=Severity.ERROR,
            detail=(
                f"integration status failed for {len(errors)} project(s); "
                "check db.migrations and daemon configuration"
            ),
            data={"projects": projections, "errors": errors},
        )

    if not projections:
        return CheckResult(
            id="integration.operational",
            severity=Severity.INFO,
            detail="no projects configured",
            data={"projects": []},
        )

    attention = [
        project
        for project in projections
        if project["draining"]
        or project["human_required"]
        or project["live_operations"]
        or project["cleanup_attention"]
        or (
            (project["effective_mode"] != "disabled" or project["desired_mode"] != "disabled")
            and project["blockers"]
        )
    ]
    if attention:
        return CheckResult(
            id="integration.operational",
            severity=Severity.WARN,
            detail=f"{len(attention)} integration project(s) require operator attention",
            data={"projects": projections},
        )

    disabled = [
        project
        for project in projections
        if project["effective_mode"] == "disabled" and project["desired_mode"] == "disabled"
    ]
    if len(disabled) == len(projections):
        return CheckResult(
            id="integration.operational",
            severity=Severity.INFO,
            detail=f"hierarchical integration is disabled for {len(disabled)} project(s)",
            data={"projects": projections},
        )
    return CheckResult(
        id="integration.operational",
        severity=Severity.OK,
        detail="enabled integration projects have no operational findings",
        data={"projects": projections},
    )


async def _check_orphaned_operations(ctx: DoctorContext) -> CheckResult:
    """Report hierarchy operations that no configured runner can advance.

    This is deliberately report-only: deciding whether every source is safely
    present on the default branch is an operator judgement.  The guard in
    ``DevelopmentIntegration.configure`` prevents creating new rows; this
    check makes any historical rows visible until they are explicitly ended.
    """
    if ctx.db is None or ctx.db._engine is None:
        return CheckResult(
            id="integration.orphaned_operations",
            severity=Severity.INFO,
            detail="integration operation scan unavailable without database",
        )

    findings: list[dict] = []
    async with ctx.db._engine.connect() as conn:
        for project in sorted(await ctx.db.list_projects(), key=lambda item: item.id):
            if project.hierarchical_integration_mode in {"hierarchy", "train"}:
                continue
            for operation in await live_operations_on(conn, project.id):
                findings.append(
                    {
                        "project_id": project.id,
                        **operation,
                        "cancel_preserving": cancel_preserving_command(operation["id"]),
                    }
                )

    if not findings:
        return CheckResult(
            id="integration.orphaned_operations",
            severity=Severity.OK,
            detail="no live hierarchy repair operations remain outside hierarchy/train",
            data={"count": 0, "operations": []},
        )

    detail = "; ".join(describe_live_operation(operation) for operation in findings)
    return CheckResult(
        id="integration.orphaned_operations",
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} live hierarchy repair operation(s) are stranded outside "
            f"hierarchy/train: {detail}"
        ),
        data={"count": len(findings), "operations": findings},
    )


def _review_dedup_key(task_id: str) -> str:
    """The dedup key ``per-task-review`` uses for its ``ensure_task``.

    Delegates to ``src/review_keys.py``, which is kept in lockstep with
    ``src/prompts/default_playbooks/default-pipeline.md`` — the check is only
    meaningful if it looks for the same row the pipeline would have written.
    """
    return review_task_dedup_key(task_id)


async def _pr_is_open(ctx: DoctorContext, project_id: str, pr_url: str) -> bool | None:
    """``True`` open, ``False`` merged/closed, ``None`` when it can't be told.

    ``None`` is a first-class answer, not a failure: doctor runs offline, in
    CI, and on machines with no ``gh`` auth.  An unverifiable PR is still
    reported — a completed task with no review is worth a warning whether or
    not the PR turns out to have been merged by hand.
    """
    handler = ctx.handler
    orchestrator = getattr(handler, "orchestrator", None) if handler else None
    git = getattr(orchestrator, "git", None)
    if git is None or ctx.db is None:
        return None
    try:
        project = await ctx.db.get_project(project_id)
        if project is None or not project.repo_url:
            return None
        repository = await git.bind_github_repository(project.repo_url)
        merged = await git.acheck_pr_merged("", pr_url, repository=repository)
    except Exception:
        # Any gh/git failure (no auth, no network, deleted repo) is "unknown".
        return None
    # ``acheck_pr_merged``: True merged, False open, None closed-unmerged.
    if merged is None:
        return False
    return not merged


async def _find_unreviewed(ctx: DoctorContext) -> list[dict]:
    """Recently COMPLETED tasks that carry a PR but have no review task."""
    all_tasks = await ctx.db.list_tasks()
    existing_reviews = {t.dedup_key for t in all_tasks if t.dedup_key}
    cutoff = time.time() - _WINDOW_SECONDS

    candidates = [
        t
        for t in all_tasks
        if t.status == TaskStatus.COMPLETED
        and t.pr_url
        and (t.updated_at or 0.0) >= cutoff
        and _review_dedup_key(t.id) not in existing_reviews
    ]
    # Newest first: if the probe budget runs out, spend it on the completions
    # most likely to still be actionable.
    candidates.sort(key=lambda t: t.updated_at or 0.0, reverse=True)

    findings: list[dict] = []
    for index, task in enumerate(candidates):
        open_pr: bool | None = None
        if index < _MAX_PR_PROBES:
            open_pr = await _pr_is_open(ctx, task.project_id, task.pr_url)
        if open_pr is False:
            # Merged or closed: the PR is not stranded, whatever happened to
            # the review task.
            continue
        findings.append(
            {
                "task_id": task.id,
                "project_id": task.project_id,
                "branch_name": task.branch_name,
                "pr_url": task.pr_url,
                "pr_open": open_pr,
                "completed_at": task.updated_at,
            }
        )
    return findings


async def _check_unreviewed_prs(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.unreviewed_prs",
            severity=Severity.INFO,
            detail="database not initialised — integration state unknown",
        )
    findings = await _find_unreviewed(ctx)
    if not findings:
        return CheckResult(
            id="integration.unreviewed_prs",
            severity=Severity.OK,
            detail="every recently completed task with a PR has a review task",
        )
    return CheckResult(
        id="integration.unreviewed_prs",
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} task(s) completed in the last 24h with an open PR "
            "and no review task — check that task.completed is reaching the "
            "review pipeline (aq playbook list-runs)"
        ),
        data={"count": len(findings), "tasks": findings[:50]},
    )


async def _find_parked_discards(ctx: DoctorContext) -> list[dict]:
    """Branch discards that stopped short of removing their ref."""
    from sqlalchemy import select

    from src.database.tables import task_branch_origins

    async with ctx.db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(task_branch_origins)
                    .where(task_branch_origins.c.discard_state.in_(("conflict", "failed")))
                    .order_by(task_branch_origins.c.discard_requested_at.desc())
                    .limit(50)
                )
            )
            .mappings()
            .all()
        )
    return [
        {
            "origin_id": row["id"],
            "task_id": row["task_id"],
            "branch": row["branch_name"],
            "state": row["discard_state"],
            "attempts": row["discard_attempts"],
            "error": row["discard_last_error"],
        }
        for row in rows
    ]


async def _check_branch_discards(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.branch_discards",
            severity=Severity.INFO,
            detail="database not initialised — branch discard state unknown",
        )
    parked = await _find_parked_discards(ctx)
    if not parked:
        return CheckResult(
            id="integration.branch_discards",
            severity=Severity.OK,
            detail="no branch discard is parked",
        )
    first = parked[0]
    return CheckResult(
        id="integration.branch_discards",
        severity=Severity.WARN,
        detail=(
            f"{len(parked)} branch discard(s) did not finish — e.g. "
            f"{first['branch'] or 'unknown branch'}: "
            f"{first['error']}. The task is already deleted; the ref is still on the "
            "remote. Restore a missing origin branch before re-arming; otherwise "
            "delete the ref by hand, or re-arm with "
            "`aq doctor --check integration.branch_discards --fix`"
        ),
        fixable=all(row["branch"] for row in parked),
        data={"count": len(parked), "discards": parked},
    )


async def _fix_branch_discards(ctx: DoctorContext) -> CheckResult:
    """Re-arm parked discards for one more pass.

    Safe to repeat: it only moves rows back to ``pending``, and the drain
    re-derives the head and the owner check from scratch.  A conflict that is
    still a conflict simply parks again.
    """
    from sqlalchemy import update

    from src.database.tables import task_branch_origins

    parked = await _find_parked_discards(ctx)
    if not parked:
        return CheckResult(
            id="integration.branch_discards",
            severity=Severity.OK,
            detail="no branch discard is parked",
        )
    if any(not row["branch"] for row in parked):
        return CheckResult(
            id="integration.branch_discards",
            severity=Severity.WARN,
            detail="restore the exact branch on origins with unknown refs before re-arming",
            data={"count": len(parked), "discards": parked},
        )
    async with ctx.db.immediate() as conn:
        await conn.execute(
            update(task_branch_origins)
            .where(task_branch_origins.c.id.in_([row["origin_id"] for row in parked]))
            .values(
                discard_state="pending",
                discard_attempts=0,
                discard_next_attempt_at=None,
                discard_last_error=None,
            )
        )
    return CheckResult(
        id="integration.branch_discards",
        severity=Severity.OK,
        detail=f"re-armed {len(parked)} branch discard(s) for another attempt",
        fixable=True,
        fix_applied=True,
        data={"count": len(parked)},
    )


#: Handoff states that mean "a writer is (or was) holding this branch".  A
#: ``reserved`` row is exactly what the next claim needs and a ``released`` one
#: is already finished, so neither can be stranded.
_HELD_HANDOFF_STATES = ("attached", "handoff_pending")

#: Task statuses that mean the owning task is actually being worked.  A row
#: held for one of these has a live writer and is none of doctor's business.
_RUNNING_TASK_STATUSES = (TaskStatus.ASSIGNED.value, TaskStatus.IN_PROGRESS.value)


async def _check_missing_canonical_owners(ctx: DoctorContext) -> CheckResult:
    """Report train producers that a claim cannot attach to their own branch."""
    check_id = "integration.missing_canonical_owners"
    if ctx.db is None:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="database not initialised — branch reservations unknown",
        )
    from sqlalchemy import and_, or_, select

    from src.database.tables import (
        integration_branch_owners,
        projects,
        task_integration_checkpoints,
        tasks,
    )

    owner = integration_branch_owners
    checkpoint = task_integration_checkpoints
    async with ctx.db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(
                        tasks.c.id,
                        tasks.c.project_id,
                        tasks.c.status,
                        tasks.c.branch_name,
                        owner.c.handoff_state,
                    )
                    .join(projects, projects.c.id == tasks.c.project_id)
                    .join(
                        checkpoint,
                        and_(
                            checkpoint.c.task_id == tasks.c.id,
                            checkpoint.c.repository_id == tasks.c.repo_id,
                            checkpoint.c.branch == tasks.c.branch_name,
                        ),
                    )
                    .outerjoin(
                        owner,
                        and_(
                            owner.c.repository_id == tasks.c.repo_id,
                            owner.c.ref == tasks.c.branch_name,
                        ),
                    )
                    .where(
                        tasks.c.status.in_(("READY", "BLOCKED")),
                        tasks.c.repo_id == projects.c.integration_repository_id,
                        projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
                        checkpoint.c.state != "verifying",
                        or_(owner.c.id.is_(None), owner.c.handoff_state == "released"),
                    )
                    .order_by(tasks.c.id)
                    .limit(50)
                )
            )
            .mappings()
            .all()
        )
    findings = [dict(row) for row in rows]
    if not findings:
        return CheckResult(
            id=check_id,
            severity=Severity.OK,
            detail="no READY/BLOCKED train task lacks its canonical reservation",
        )
    return CheckResult(
        id=check_id,
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} READY/BLOCKED train task(s) lack a canonical branch "
            "reservation; run `aq integration reserve-owner --task-id <id>` "
            "for each task after confirming its old writer stopped."
        ),
        data={"count": len(findings), "tasks": findings},
    )


async def _find_stranded_fences(ctx: DoctorContext) -> list[dict]:
    """Ownership rows still held for a task that has no writer left.

    A close that returns a task to the frontier is supposed to put its
    ownership row back to ``reserved`` before the claim release erases the
    evidence any handoff proof reads.  When that does not happen -- the bug
    fair-willow fixes, and any future one shaped like it -- the row stays
    ``attached`` with nothing attached to it, and because
    ``_integration_owner_fence`` requires ``reserved``, *every* subsequent
    claim of that same task dies with "canonical branch is not reserved by
    this task" and burns a pool worker.  The task stays READY, so the
    scheduler keeps offering it: the cost repeats until someone notices.

    A row is reported only when the database says no writer is left: no live
    session for the owning task, no workspace still locked by it, and the task
    itself is not ASSIGNED/IN_PROGRESS (or is gone entirely).  ``collector``
    rows are excluded -- their owner is an operation, not a task, so none of
    those questions are even askable of them.

    That is a *diagnosis*, not a licence to write.  None of it proves the
    writer's provider is actually stopped, that its checkout is clean, or that
    whatever the checkout holds has been published, and a row can also be
    rebound by a guarded recovery path without any of the fields above
    changing.  So this stays a report: the operator repair is the guarded
    integration recovery path, which takes those proofs.
    """
    from sqlalchemy import select

    from src.database.tables import integration_branch_owners, sessions, tasks, workspaces

    async with ctx.db._engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    select(integration_branch_owners)
                    .where(
                        integration_branch_owners.c.handoff_state.in_(_HELD_HANDOFF_STATES),
                        integration_branch_owners.c.owner_role != "collector",
                    )
                    .order_by(integration_branch_owners.c.updated_at.desc())
                    .limit(50)
                )
            )
            .mappings()
            .all()
        )
        stranded = []
        for row in rows:
            task = (
                await conn.execute(select(tasks).where(tasks.c.id == row["owner_id"]))
            ).mappings().one_or_none()
            if task is not None and task["status"] in _RUNNING_TASK_STATUSES:
                continue
            live_session = (
                await conn.execute(
                    select(sessions.c.id)
                    .where(sessions.c.task_id == row["owner_id"], session_attached_clause())
                    .limit(1)
                )
            ).scalar_one_or_none()
            if live_session is not None:
                continue
            held_workspace = (
                await conn.execute(
                    select(workspaces.c.id)
                    .where(workspaces.c.locked_by_task_id == row["owner_id"])
                    .limit(1)
                )
            ).scalar_one_or_none()
            if held_workspace is not None:
                continue
            stranded.append(
                {
                    "owner_row_id": row["id"],
                    "repository_id": row["repository_id"],
                    "ref": row["ref"],
                    "task_id": row["owner_id"],
                    "owner_role": row["owner_role"],
                    "fence_token": int(row["fence_token"]),
                    "handoff_state": row["handoff_state"],
                    "task_status": task["status"] if task is not None else None,
                }
            )
    return stranded


async def _check_stranded_fences(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.stranded_fences",
            severity=Severity.INFO,
            detail="database not initialised — branch ownership state unknown",
        )
    stranded = await _find_stranded_fences(ctx)
    if not stranded:
        return CheckResult(
            id="integration.stranded_fences",
            severity=Severity.OK,
            detail="no integration branch is held by a writer that is gone",
        )
    first = stranded[0]
    return CheckResult(
        id="integration.stranded_fences",
        severity=Severity.WARN,
        detail=(
            f"{len(stranded)} integration branch(es) look held '{first['handoff_state']}' by a "
            f"writer that no longer exists — e.g. {first['ref']} for task "
            f"{first['task_id']} ({first['owner_role']}, task status "
            f"{first['task_status'] or 'gone'}). Every claim of that task fails "
            "'canonical branch is not reserved by this task'. Recover it with "
            "`aq doctor --check integration.stranded_fences --fix`; the guarded recovery "
            "path re-proves the writer stopped and the checkout is safe before releasing it."
        ),
        fixable=True,
        data={"count": len(stranded), "fences": stranded},
    )


async def _fix_stranded_fences(ctx: DoctorContext) -> CheckResult:
    """Run guarded recovery for every row currently diagnosed as stranded."""
    recovery = owner_recovery_for(getattr(ctx.handler, "orchestrator", None))
    if recovery is None:
        result = await _check_stranded_fences(ctx)
        result.data["fix_unavailable"] = "orchestrator not available"
        return result

    stranded = await _find_stranded_fences(ctx)
    outcomes = await recovery.recover_many(
        [row["owner_row_id"] for row in stranded], principal="doctor"
    )
    result = await _check_stranded_fences(ctx)
    result.fixable = True
    result.fix_applied = any(
        outcome.outcome in {"released", "preserved_and_released"} for outcome in outcomes
    )
    result.data["outcomes"] = [outcome.to_dict() for outcome in outcomes]
    return result


async def _find_stranded_dependents(
    ctx: DoctorContext,
    *,
    candidate_statuses: tuple[TaskStatus, ...] = (TaskStatus.COMPLETED,),
) -> list[dict]:
    """Find candidates held behind a cleaned-up commits-less delivery.

    A delivered manifest normally makes its source durable enough to survive
    branch cleanup. Older publisher builds consulted an empty completion first,
    however, and silently marked that blocker unavailable. The cleanup receipt
    is the durable proof that this is that specific failure mode; a merely
    completed task with no commits is not enough to warrant an alarm.
    """
    from sqlalchemy import select

    from src.database.tables import (
        development_deliveries,
        projects,
        task_completion_records,
        task_dependencies,
        tasks,
    )

    async with ctx.db._engine.connect() as conn:
        development = set(
            (
                await conn.execute(
                    select(projects.c.id).where(
                        projects.c.hierarchical_integration_mode == "development"
                    )
                )
            ).scalars()
        )
        if not development:
            return []
        candidates = (
            (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id)
                    .where(
                        tasks.c.project_id.in_(development),
                        tasks.c.status.in_(tuple(status.value for status in candidate_statuses)),
                    )
                )
            )
            .mappings()
            .all()
        )
        candidate_projects = {row["id"]: row["project_id"] for row in candidates}
        if not candidate_projects:
            return []
        links = (
            (
                await conn.execute(
                    select(task_dependencies.c.task_id, task_dependencies.c.depends_on_task_id)
                    .where(
                        task_dependencies.c.task_id.in_(candidate_projects),
                        task_dependencies.c.dep_type.in_(("blocks", "waits-for", "conditional-blocks")),
                    )
                )
            )
            .mappings()
            .all()
        )
        blocker_ids = {link["depends_on_task_id"] for link in links}
        if not blocker_ids:
            return []
        blockers = {
            row["id"]: row
            for row in (
                (
                    await conn.execute(
                        select(tasks.c.id, tasks.c.project_id, tasks.c.status)
                        .where(tasks.c.id.in_(blocker_ids))
                    )
                )
                .mappings()
                .all()
            )
        }
        completions = {}
        for row in (
            (
                await conn.execute(
                    select(
                        task_completion_records.c.task_id,
                        task_completion_records.c.id,
                        task_completion_records.c.commits,
                    )
                    .where(task_completion_records.c.task_id.in_(blocker_ids))
                    .order_by(
                        task_completion_records.c.task_id,
                        task_completion_records.c.completed_at.desc(),
                        task_completion_records.c.id.desc(),
                    )
                )
            )
            .mappings()
            .all()
        ):
            completions.setdefault(row["task_id"], row)
        rows = (
            (
                await conn.execute(
                    select(development_deliveries)
                    .where(
                        development_deliveries.c.project_id.in_(development),
                        development_deliveries.c.state.in_(("delivered", "adopted")),
                    )
                    .order_by(
                        development_deliveries.c.created_at.desc(),
                        development_deliveries.c.id.desc(),
                    )
                )
            )
            .mappings()
            .all()
        )

    delivered_task_ids = {
        member.get("task_id")
        for row in rows
        for member in row["manifest"] or []
        if isinstance(member, dict) and member.get("task_id")
    }
    stranded = {}
    for row in rows:
        cleanup = (row["evidence"] or {}).get("branch_cleanup") or {}
        deleted = cleanup.get("deleted") or []
        if not deleted:
            continue
        for member in row["manifest"] or []:
            blocker_id = member.get("task_id")
            source_sha = member.get("source_sha")
            blocker = blockers.get(blocker_id)
            completion = completions.get(blocker_id)
            if (
                blocker is None
                or blocker["project_id"] != row["project_id"]
                or blocker["status"] != TaskStatus.COMPLETED.value
                or completion is None
                or json.loads(completion["commits"])
                or not any(entry.get("sha") == source_sha for entry in deleted)
            ):
                continue
            for link in links:
                if link["depends_on_task_id"] != blocker_id:
                    continue
                dependent_id = link["task_id"]
                if (
                    candidate_projects.get(dependent_id) != row["project_id"]
                    or dependent_id in delivered_task_ids
                ):
                    continue
                key = (dependent_id, blocker_id)
                stranded.setdefault(
                    key,
                    {
                        "project_id": row["project_id"],
                        "dependent_task_id": dependent_id,
                        "blocker_task_id": blocker_id,
                        "delivery_id": row["id"],
                        "source_sha": source_sha,
                    },
                )
    return sorted(stranded.values(), key=lambda item: (item["project_id"], item["dependent_task_id"]))


async def _check_stranded_dependents(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.stranded_dependents",
            severity=Severity.INFO,
            detail="database not initialised — development dependency state unknown",
        )
    findings = await _find_stranded_dependents(ctx)
    if not findings:
        return CheckResult(
            id="integration.stranded_dependents",
            severity=Severity.OK,
            detail="no completed development candidate is stranded behind a cleaned-up delivery",
            data={"count": 0, "dependents": []},
        )
    first = findings[0]
    return CheckResult(
        id="integration.stranded_dependents",
        severity=Severity.ERROR,
        detail=(
            f"{len(findings)} completed development candidate(s) are stranded behind a "
            f"cleaned-up commits-less delivery — e.g. {first['dependent_task_id']} waits for "
            f"{first['blocker_task_id']} ({first['source_sha']}). Restore the blocker's branch "
            "at its delivered SHA, then let the development publisher sweep again"
        ),
        fixable=False,
        data={"count": len(findings), "dependents": findings[:50]},
    )


async def _find_publisher_stalls(ctx: DoctorContext) -> list[dict]:
    """Durable symptoms of a development publisher that stopped making progress.

    They are read straight out of state the publisher already writes, so the
    check works against a stopped daemon and needs no process-local memory.
    """
    from sqlalchemy import func, select

    from src.database.tables import (
        archived_tasks,
        development_deliveries,
        projects,
        task_completion_records,
        task_metadata,
        tasks,
    )

    async with ctx.db._engine.connect() as conn:
        development = set(
            (
                await conn.execute(
                    select(projects.c.id).where(
                        projects.c.hierarchical_integration_mode == "development"
                    )
                )
            ).scalars()
        )
        if not development:
            return []
        rows = (
            (
                await conn.execute(
                    select(
                        development_deliveries.c.id,
                        development_deliveries.c.project_id,
                        development_deliveries.c.state,
                        development_deliveries.c.manifest,
                        development_deliveries.c.evidence,
                    ).where(development_deliveries.c.project_id.in_(development))
                )
            )
            .mappings()
            .all()
        )
        owners = dict(
            (
                await conn.execute(
                    select(tasks.c.id, tasks.c.project_id).where(
                        tasks.c.id.like("development-repair-%")
                    )
                )
            ).all()
        )
        for task_id, project_id in (
            await conn.execute(
                select(archived_tasks.c.id, archived_tasks.c.project_id).where(
                    archived_tasks.c.id.like("development-repair-%")
                )
            )
        ).all():
            owners.setdefault(task_id, project_id)
        passing = (
            await conn.execute(
                select(
                    task_completion_records.c.task_id,
                    func.max(task_completion_records.c.completed_at),
                )
                .where(
                    task_completion_records.c.outcome == "pass",
                    task_completion_records.c.task_id.like("development-repair-%"),
                )
                .group_by(task_completion_records.c.task_id)
            )
        ).all()
        skips = (
            await conn.execute(
                select(tasks.c.id, tasks.c.project_id, task_metadata.c.value)
                .select_from(task_metadata.join(tasks, task_metadata.c.task_id == tasks.c.id))
                .where(
                    task_metadata.c.key == "development_publisher_skip",
                    tasks.c.project_id.in_(development),
                    tasks.c.status == TaskStatus.COMPLETED.value,
                )
            )
        ).all()

    findings, collected = [], set()
    for row in rows:
        for member in row["manifest"] or []:
            # Any manifest counts: a source the publisher picked up and then
            # parked was collected.  "Uncollected" means never picked up.
            collected.add((row["project_id"], member["task_id"]))
    for row in rows:
        streak = _validation_infrastructure_stall(row)
        if streak is not None:
            findings.append(streak)
            continue
        if row["state"] not in {"parked", "prepared", "publishing"}:
            continue
        diagnostic = (row["evidence"] or {}).get("publisher_diagnostic") or {}
        ticks = diagnostic.get("consecutive_ticks", 0)
        if ticks < _STALL_TICKS:
            continue
        findings.append(
            {
                "project_id": row["project_id"],
                "batch_id": row["id"],
                "cause": diagnostic.get("kind", "unknown"),
                "detail": diagnostic.get("detail", ""),
                "consecutive_ticks": ticks,
                "task_ids": diagnostic.get("task_ids", []),
                "first_failed_at": diagnostic.get("first_failed_at"),
            }
        )

    cutoff = time.time() - _UNCOLLECTED_AFTER_SECONDS
    for task_id, completed_at in passing:
        project_id = owners.get(task_id)
        if project_id not in development or (completed_at or 0) > cutoff:
            continue
        if (project_id, task_id) in collected:
            continue
        findings.append(
            {
                "project_id": project_id,
                "batch_id": None,
                "cause": "repair_branch_uncollected",
                "detail": (
                    f"repair {task_id} closed pass on branch aq/{task_id} "
                    f"{int((time.time() - (completed_at or 0)) // 60)} minute(s) ago and has "
                    "not appeared in any development batch manifest"
                ),
                "consecutive_ticks": None,
                "task_ids": [task_id],
                "first_failed_at": completed_at,
            }
        )
    for task_id, project_id, raw in skips:
        try:
            skip = json.loads(raw)
        except (TypeError, ValueError):
            continue
        ticks = skip.get("consecutive_ticks", 0)
        if ticks < _SKIP_STALL_TICKS:
            continue
        dependency_id = skip.get("dependency_id")
        reason = skip.get("reason", "unknown")
        findings.append({
            "project_id": project_id,
            "batch_id": None,
            "cause": "candidate_skipped",
            "detail": (
                f"child {task_id} skipped because dependency {dependency_id} is {reason}"
                if dependency_id else f"child {task_id} skipped: {reason}"
            ),
            "consecutive_ticks": ticks,
            "task_ids": [task_id],
            "dependency_id": dependency_id,
            "reason": reason,
            "first_failed_at": skip.get("first_skipped_at"),
        })
    findings.sort(key=lambda f: (f["first_failed_at"] or 0))
    return findings


def _validation_infrastructure_stall(row) -> dict | None:
    """A development validation that keeps failing to *finish*.

    The publisher defers a batch whose validation verified nothing (timeout,
    no test slot, an outage, nothing collected) instead of parking it and
    filing a repair.  Its open streak row counts the deferrals; past the
    alert threshold nothing will clear it but a fixed environment.
    """
    from src.integration.development_validation import DEFERRAL_KIND, INFRA_ALERT_AFTER

    evidence = row["evidence"] or {}
    if (
        row["state"] != "cancelled"
        or evidence.get("kind") != DEFERRAL_KIND
        or not evidence.get("open")
    ):
        return None
    consecutive = evidence.get("consecutive", 0)
    if consecutive < INFRA_ALERT_AFTER:
        return None
    latest = (evidence.get("runs") or [{}])[-1]
    return {
        "project_id": row["project_id"],
        "batch_id": row["id"],
        "cause": "validation_infrastructure",
        "detail": (
            f"validation could not finish {consecutive} times in a row "
            f"({latest.get('reason', 'unknown')}: {latest.get('detail', '')}); "
            "the batch is deferred and no repair was filed"
        ),
        "consecutive_ticks": consecutive,
        "task_ids": latest.get("members", []),
        "first_failed_at": evidence.get("first_at"),
    }


async def _check_publisher_stalled(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.development_publisher_stalled",
            severity=Severity.INFO,
            detail="database not initialised — development publisher state unknown",
        )
    stalls = await _find_publisher_stalls(ctx)
    if not stalls:
        return CheckResult(
            id="integration.development_publisher_stalled",
            severity=Severity.OK,
            detail="every development publisher is making progress",
        )
    first = stalls[0]
    where = f"batch {first['batch_id']}" if first["batch_id"] else f"project {first['project_id']}"
    skipped_only = all(stall["cause"] == "candidate_skipped" for stall in stalls)
    if skipped_only:
        advice = (
            f"Run `aq integration sweep {first['project_id']} --recover-child "
            f"{first['task_ids'][0]}` to retry and verify publication"
        )
    elif first["cause"] == "validation_infrastructure":
        advice = (
            "Fix the validation environment (test database, test slots, "
            "timeout_seconds / slot_wait_seconds); the deferral row in "
            f"`aq integration status {first['project_id']}` holds each run's output"
        )
    else:
        advice = (
            "The publisher cannot clear this by itself; read the batch with "
            "`aq integration status` and resolve or cancel it"
        )
    return CheckResult(
        id="integration.development_publisher_stalled",
        severity=Severity.WARN if skipped_only else Severity.ERROR,
        detail=(
            f"{len(stalls)} development publisher stall(s) — e.g. {where} "
            f"({first['project_id']}): {first['cause']}: {first['detail']}. {advice}"
        ),
        fixable=False,
        data={"count": len(stalls), "stalls": stalls[:50]},
    )


async def _find_stranded_delegates(ctx: DoctorContext) -> list[dict]:
    """Delegate tickets of an integration operation that has already ended.

    A verifier, a repair-stage writer or a candidate-member resolver whose
    operation was cancelled, superseded or completed without it will never be
    scheduled, closed or waited on again.  Left unsettled it sits DEFINED,
    READY, BLOCKED or PAUSED forever, counts against the project's open work,
    and refuses to archive.

    Narrow on purpose: a task is *not* listed merely because integration
    history still names it — that is the integration-history removal guard's
    question, and listing every historical verifier would bury the ones that
    are genuinely stuck.  A delegate with a live writer is also excluded — its
    authority is not doctor's to take.
    """
    from src.integration.delegate_release import stranded_delegates

    return await stranded_delegates(ctx.db, limit=200)


async def _check_stranded_delegates(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.stranded_delegates",
            severity=Severity.INFO,
            detail="database not initialised — integration delegate state unknown",
        )
    stranded = await _find_stranded_delegates(ctx)
    if not stranded:
        return CheckResult(
            id="integration.stranded_delegates",
            severity=Severity.OK,
            detail="no delegate is held open by an integration operation that ended",
        )
    first = stranded[0]
    return CheckResult(
        id="integration.stranded_delegates",
        severity=Severity.WARN,
        detail=(
            f"{len(stranded)} task(s) are delegates of an integration operation that already "
            f"ended — e.g. {first['task_id']} ({first['task_status']}, {first['role']} of "
            f"operation {first['operation_id']}, which is {first['operation_state']}). "
            "Nothing will ever schedule or close them. Settle them with "
            "`aq doctor --check integration.stranded_delegates --fix`, which retires each "
            "ticket as a non-success and records why in integration_delegate_releases"
        ),
        fixable=True,
        data={"count": len(stranded), "delegates": stranded},
    )


async def _fix_stranded_delegates(ctx: DoctorContext) -> CheckResult:
    """Retire each stranded delegate and record the release.

    Safe to repeat: a delegate that is already terminal is not selected again,
    so a second run reports clean rather than writing a second release.  It
    settles the *ticket* only — a retained branch owner or workspace lock is
    preserved exactly as found and recorded as a named cleanup blocker, because
    releasing either needs proof doctor cannot take.
    """
    import time as _time

    from src.integration.delegate_release import release_delegates

    stranded = await _find_stranded_delegates(ctx)
    if not stranded:
        return CheckResult(
            id="integration.stranded_delegates",
            severity=Severity.OK,
            detail="no delegate is held open by an integration operation that ended",
        )
    released = await release_delegates(
        ctx.db, now=_time.time(), released_by="doctor", limit=200
    )
    blocked = [row for row in released if row["cleanup"]["state"] == "blocked"]
    detail = (
        f"released {len(released)} stranded delegate(s); each is terminal FAILED with its "
        "operation and disposition recorded in integration_delegate_releases"
    )
    if blocked:
        detail += (
            f". {len(blocked)} still hold a branch owner or workspace lock — see "
            "`aq task explain <id>`; releasing those needs the guarded integration "
            "recovery path, not doctor"
        )
    return CheckResult(
        id="integration.stranded_delegates",
        severity=Severity.OK,
        detail=detail,
        fixable=True,
        fix_applied=True,
        data={"count": len(released), "released": released},
    )


#: ``integration.sweep_due`` retries this many times before an undelivered
#: sweep counts as stuck rather than merely queued.
_UNDELIVERED_SWEEP_ATTEMPTS = 3


async def _find_stale_schedules(ctx: DoctorContext) -> list[dict]:
    """Train schedules whose outstanding request nothing will end on its own.

    ``stale`` and ``unsealed`` requests coalesce every later flush and tick into
    a sweep that never runs; ``blocked`` ones would too, but their batch still
    carries unresolved write evidence.  A request whose ``integration.sweep_due``
    no playbook accepts after repeated attempts is listed as well: it is not
    stale, yet no sweep will run until the route accepts it.
    """
    from sqlalchemy import select

    from src.database.tables import project_integration_schedules, projects
    from src.integration.stale_schedule import classify_outstanding_request_on

    now = time.time()
    findings = []
    async with ctx.db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    project_integration_schedules,
                    projects.c.hierarchical_integration_mode.label("mode"),
                )
                .join(projects, projects.c.id == project_integration_schedules.c.project_id)
                .where(project_integration_schedules.c.outstanding_request_id.is_not(None))
                .order_by(project_integration_schedules.c.project_id)
                .limit(200)
            )
        ).mappings().all()
        for row in rows:
            state = await classify_outstanding_request_on(
                conn, row["project_id"], row, now=now
            )
            undelivered = (
                state.verdict == "in_flight"
                and state.event is not None
                and state.event["attempts"] >= _UNDELIVERED_SWEEP_ATTEMPTS
                and state.event["last_error"]
            )
            if state.verdict in {"stale", "unsealed", "blocked"} or undelivered:
                findings.append(
                    {
                        **state.as_dict(),
                        "mode": row["mode"],
                        "requested_at": row["outstanding_requested_at"],
                        "next_due_at": row["next_due_at"],
                        "last_completed_sweep_at": row["last_completed_sweep_at"],
                    }
                )
    return findings


def _stale_schedule_ok() -> CheckResult:
    return CheckResult(
        id="integration.stale_schedule",
        severity=Severity.OK,
        detail="every outstanding train sweep request can still end on its own",
    )


async def _check_stale_schedule(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.stale_schedule",
            severity=Severity.INFO,
            detail="database not initialised — integration schedule state unknown",
        )
    findings = await _find_stale_schedules(ctx)
    if not findings:
        return _stale_schedule_ok()
    first = findings[0]
    subject = (
        f"batch {first['batch_id']} ({first['lifecycle']})"
        if first["batch_id"]
        else "no batch"
    )
    return CheckResult(
        id="integration.stale_schedule",
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} train schedule(s) hold a sweep request that will not end on "
            f"its own — e.g. {first['project_id']}: {first['request_id']} is "
            f"{first['verdict']} ({subject}): {first['reason']}. Every flush answers "
            "`coalesced` and no sweep runs meanwhile. `stale` requests are freed by the "
            "scheduler's next due tick, or now by `aq doctor --check "
            "integration.stale_schedule --fix`; `aq integration clear-stale-request "
            "<project>` also frees `unsealed` ones. `blocked` names unresolved write "
            "evidence to settle first"
        ),
        fixable=any(item["verdict"] == "stale" for item in findings),
        data={"count": len(findings), "schedules": findings},
    )


async def _fix_stale_schedule(ctx: DoctorContext) -> CheckResult:
    """Free each ``stale`` request exactly as the scheduler's next pass would.

    Leaves ``unsealed`` (the seal may still be queued), ``blocked`` and
    undelivered requests alone: those are the operator's judgement.
    """
    from src.integration.stale_schedule import release_stale_request

    findings = await _find_stale_schedules(ctx)
    cleared = []
    for item in findings:
        if item["verdict"] != "stale":
            continue
        result = await release_stale_request(
            ctx.db,
            item["project_id"],
            now=time.time(),
            released_by="doctor",
            reason=f"aq doctor --check integration.stale_schedule --fix: {item['reason']}",
            expected_request_id=item["request_id"],
        )
        if result["outcome"] == "cleared":
            cleared.append({"project_id": item["project_id"], **result["release"]})
    if not findings:
        return _stale_schedule_ok()
    remaining = len(findings) - len(cleared)
    detail = f"freed {len(cleared)} stale train sweep request(s)"
    if remaining:
        detail += (
            f"; {remaining} left for an operator (unsealed, blocked or undelivered) — see "
            "`aq integration clear-stale-request <project>`"
        )
    return CheckResult(
        id="integration.stale_schedule",
        severity=Severity.WARN if remaining else Severity.OK,
        detail=detail,
        fixable=True,
        fix_applied=bool(cleared),
        data={"count": len(cleared), "cleared": cleared},
    )


_STUCK_CHILD_CAUSES = {
    "none": (
        "no approved evidence pins its head; the collector records completion evidence "
        "once the published branch proves out, so a child still here has a head that "
        "does not (moved or unpublished branch, or an open reviewer)"
    ),
    "rejected": "a reviewer rejected its head; the child must be reworked",
    "approved": (
        "approved but not queued; the parent's collector fence or a sibling's promotion "
        "is holding it"
    ),
}


async def _find_stuck_children(ctx: DoctorContext) -> list[dict]:
    """COMPLETED children of collecting parents that were never assembled.

    Each one keeps every sibling whose ``needs`` names it out of the claim
    frontier (``delivered_same_parent_prerequisites_when_hierarchical``), so a
    parent can show READY children that no pool will ever claim.
    """
    from src.integration.child_delivery import latest_evidence_on, stuck_children_statement

    now = time.time()
    findings = []
    async with ctx.db._engine.connect() as conn:
        rows = (
            await conn.execute(
                stuck_children_statement(updated_before=now - _STUCK_CHILD_AFTER_SECONDS)
            )
        ).mappings().all()
        for row in rows:
            latest = await latest_evidence_on(
                conn,
                task_id=row["task_id"],
                repository_id=row["repository_id"],
                base_sha=row["base_sha"],
                head_sha=row["head_sha"],
                generation=int(row["generation"]),
            )
            evidence = latest["verdict"] if latest is not None else "none"
            findings.append(
                {
                    "task_id": row["task_id"],
                    "project_id": row["project_id"],
                    "parent_task_id": row["parent_task_id"],
                    "branch": row["branch"],
                    "head_sha": row["head_sha"],
                    "evidence": evidence,
                    "cause": _STUCK_CHILD_CAUSES[evidence],
                    "waiting_seconds": round(
                        now - max(row["updated_at"], row["checkpoint_updated_at"])
                    ),
                }
            )
    return findings


async def _check_blocked_collectors(ctx: DoctorContext) -> CheckResult:
    """Expose parents that the PAUSED-only collection scan cannot see."""
    from sqlalchemy import and_, select

    from src.database.tables import (
        integration_repair_operations,
        projects,
        task_integration_checkpoints,
        tasks,
    )

    check_id = "integration.blocked_collectors"
    if ctx.db is None:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="database not initialised — parent collection state unknown",
        )
    checkpoint = task_integration_checkpoints
    operation = integration_repair_operations
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(
            select(
                tasks.c.id.label("task_id"), tasks.c.project_id,
                checkpoint.c.episode_id, checkpoint.c.generation,
                operation.c.id.label("operation_id"),
                operation.c.state.label("operation_state"),
            )
            .join(checkpoint, checkpoint.c.task_id == tasks.c.id)
            .join(projects, projects.c.id == tasks.c.project_id)
            .outerjoin(operation, and_(
                operation.c.parent_task_id == tasks.c.id,
                operation.c.episode_id == checkpoint.c.episode_id,
            ))
            .where(
                tasks.c.status == TaskStatus.BLOCKED.value,
                checkpoint.c.state == "awaiting_children",
                projects.c.hierarchical_integration_mode.in_(("hierarchy", "train")),
            )
            .order_by(tasks.c.id)
        )).mappings().all()
    findings = [dict(row) for row in rows]
    return CheckResult(
        id=check_id,
        severity=Severity.WARN if findings else Severity.OK,
        detail=(
            f"{len(findings)} BLOCKED parent(s) still await children; collection only scans "
            "PAUSED parents. Inspect the episode, operation and transition context before "
            "recovery; human_required repair operations need explicit integration resume"
            if findings else "no BLOCKED parent is stranded awaiting children"
        ),
        data={"count": len(findings), "parents": findings},
    )


async def _check_stuck_children(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.stuck_children",
            severity=Severity.INFO,
            detail="database not initialised — child collection state unknown",
        )
    findings = await _find_stuck_children(ctx)
    if not findings:
        return CheckResult(
            id="integration.stuck_children",
            severity=Severity.OK,
            detail="every completed child of a collecting parent has been assembled",
        )
    first = findings[0]
    return CheckResult(
        id="integration.stuck_children",
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} completed child task(s) have waited more than "
            f"{_STUCK_CHILD_AFTER_SECONDS // 60} minutes for their parent to assemble them "
            f"— e.g. {first['task_id']} (parent {first['parent_task_id']}, head "
            f"{first['head_sha'][:9]}): {first['cause']}. Siblings that need them stay "
            "out of the claim frontier. `aq integration redrive-child <task>` says why one "
            "is stuck; `--apply --head <sha> --reason ...` advances it"
        ),
        data={"count": len(findings), "children": findings},
    )


def _stop_confirmer(ctx: DoctorContext):
    from src.integration.finished_owners import stop_confirmer_for

    return stop_confirmer_for(getattr(ctx.handler, "orchestrator", None))


def _describe_owner(finding: dict) -> str:
    return (
        f"{finding['ref']} for {finding['owner_role']} {finding['owner_id']} "
        f"({finding['owner_status']}, {finding['handoff_state']})"
    )


async def _check_finished_branch_owners(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id="integration.finished_branch_owners",
            severity=Severity.INFO,
            detail="database not initialised — branch ownership state unknown",
        )
    from src.integration.finished_owners import finished_branch_owners

    findings = await finished_branch_owners(ctx.db, confirm_stopped=_stop_confirmer(ctx))
    releasable = [f for f in findings if f["blocker"] is None]
    kept = [f for f in findings if f["blocker"] is not None]
    data = {
        "count": len(releasable),
        "kept_count": len(kept),
        "releasable": releasable[:50],
        "kept": kept[:50],
    }
    if not findings:
        return CheckResult(
            id="integration.finished_branch_owners",
            severity=Severity.OK,
            detail="no branch owner row is held for a task that finished or is gone",
        )
    if not releasable:
        first = kept[0]
        return CheckResult(
            id="integration.finished_branch_owners",
            severity=Severity.INFO,
            detail=(
                f"{len(kept)} branch owner row(s) held for a finished or deleted task are "
                f"kept on purpose — e.g. {_describe_owner(first)}: {first['blocker']}"
            ),
            data=data,
        )
    first = releasable[0]
    detail = (
        f"{len(releasable)} branch owner row(s) are still held for a task that finished or "
        f"is gone — e.g. {_describe_owner(first)}. Nothing will ever release them, and "
        "branch cleanup keeps every branch a row that is not released still names. "
        "Release them with `aq doctor --check integration.finished_branch_owners --fix`, "
        "which changes only the ownership rows and records each one as an "
        "integration.branch_owner_released event"
    )
    if kept:
        detail += (
            f"; {len(kept)} more are kept — e.g. {_describe_owner(kept[0])}: {kept[0]['blocker']}"
        )
    return CheckResult(
        id="integration.finished_branch_owners",
        severity=Severity.WARN,
        detail=detail,
        fixable=True,
        data=data,
    )


async def _fix_finished_branch_owners(ctx: DoctorContext) -> CheckResult:
    """Release each owner row the check clears, re-proving it under lock.

    Safe to repeat: a released row is never selected again, so a second run
    reports clean rather than writing a second event.
    """
    from src.integration.finished_owners import release_finished_branch_owners

    released = await release_finished_branch_owners(
        ctx.db, confirm_stopped=_stop_confirmer(ctx), released_by="doctor"
    )
    return CheckResult(
        id="integration.finished_branch_owners",
        severity=Severity.OK,
        detail=(
            f"released {len(released)} branch owner row(s) held for a finished or deleted "
            "task; each is recorded as an integration.branch_owner_released event"
        ),
        fixable=True,
        fix_applied=True,
        data={"count": len(released), "released": released[:50]},
    )


async def _check_reused_task_identity(ctx: DoctorContext) -> CheckResult:
    """Report live origins that predate the task now using their identity.

    Owner timestamps cannot establish this: transferring a released branch
    preserves its created_at even when it changes owner_id. An origin's
    creation time and task identity, by contrast, are immutable together.
    """
    from sqlalchemy import select

    from src.database.tables import task_branch_origins, task_integration_checkpoints, tasks

    check_id = "integration.reused_task_identity"
    if ctx.db is None:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="database not initialised — integration identity state unknown",
        )
    origin = task_branch_origins
    checkpoint = task_integration_checkpoints
    async with ctx.db._engine.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    tasks.c.id.label("task_id"),
                    tasks.c.project_id,
                    tasks.c.status.label("task_status"),
                    tasks.c.created_at.label("task_created_at"),
                    tasks.c.repo_id.label("task_repository_id"),
                    tasks.c.branch_name.label("task_branch"),
                    origin.c.id.label("origin_id"),
                    origin.c.repository_id,
                    origin.c.branch_name.label("origin_branch"),
                    origin.c.base_sha,
                    origin.c.materialized,
                    origin.c.created_at.label("origin_created_at"),
                    checkpoint.c.repository_id.label("checkpoint_repository_id"),
                    checkpoint.c.branch.label("checkpoint_branch"),
                    checkpoint.c.checkpoint_sha,
                )
                .select_from(
                    tasks.join(origin, origin.c.task_id == tasks.c.id).outerjoin(
                        checkpoint, checkpoint.c.task_id == tasks.c.id
                    )
                )
                .where(
                    origin.c.retired_at.is_(None),
                    origin.c.created_at < tasks.c.created_at,
                )
                .order_by(tasks.c.project_id, tasks.c.id, origin.c.id)
            )
        ).mappings().all()
    findings = [dict(row) for row in rows]
    task_count = len({row["task_id"] for row in findings})
    data = {"count": len(findings), "task_count": task_count, "origins": findings[:50]}
    if not findings:
        return CheckResult(
            id=check_id,
            severity=Severity.OK,
            detail="no live branch origin predates its current task",
            data=data,
        )
    first = findings[0]
    return CheckResult(
        id=check_id,
        severity=Severity.WARN,
        detail=(
            f"{len(findings)} live origin(s) predate {task_count} current task(s) — "
            f"e.g. {first['origin_id']} for {first['task_id']} ({first['task_status']}). "
            "Suspected reused task identity; operator review of the exact branch and "
            "integration history is required before rebinding. Releasing a fence alone "
            "does not repair the origin or checkpoint: `aq integration "
            f"rebind-reused-identity --task-id {first['task_id']}` proves it (a dry run) "
            "and rebinds it with --apply."
        ),
        data=data,
    )


def integration_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="integration.operational",
            run=_check_operational,
            owner=OWNER,
        ),
        # Report-only.  Cancelling an operation is safe only after an operator
        # verifies its subtree has reached the default branch.
        DoctorCheck(
            id="integration.orphaned_operations",
            run=_check_orphaned_operations,
            owner=OWNER,
        ),
        # Report-only: a timestamp anomaly cannot authorize replacing an
        # immutable origin, clearing a checkpoint, or resetting a branch.
        DoctorCheck(
            id="integration.reused_task_identity",
            run=_check_reused_task_identity,
            owner=OWNER,
        ),
        # Report-only: no ``fix``.  Back-filling review tasks by hand would
        # paper over whatever stopped the pipeline, and the right repair
        # (re-emit the events, or merge the backlog in dependency order) is an
        # operator decision this check exists to prompt, not to make.
        #
        # ``timeout_s`` is raised above the 5s default because the check may
        # shell out to ``gh pr view`` once per candidate.
        DoctorCheck(
            id="integration.unreviewed_prs",
            run=_check_unreviewed_prs,
            owner=OWNER,
            timeout_s=30.0,
        ),
        # Fixable, but the fix only re-arms: it never deletes a ref itself.
        # A discard parks as ``conflict`` precisely when the remote stopped
        # matching what the operator asked about, and doctor is not the place
        # to overrule that.
        DoctorCheck(
            id="integration.branch_discards",
            run=_check_branch_discards,
            fix=_fix_branch_discards,
            owner=OWNER,
        ),
        # Fixable through OwnerRecovery, not by mutating the diagnostic
        # snapshot.  The recovery service proves the writer stopped and the
        # checkout safe under its own guarded transaction before it releases
        # any ownership row.
        DoctorCheck(
            id="integration.stranded_fences",
            run=_check_stranded_fences,
            fix=_fix_stranded_fences,
            owner=OWNER,
            timeout_s=60.0,
        ),
        DoctorCheck(
            id="integration.missing_canonical_owners",
            run=_check_missing_canonical_owners,
            owner=OWNER,
        ),
        # Report-only. A historical publisher skipped completed work when a
        # delivered blocker's branch had already been cleaned up and its close
        # listed no commits. The delivery receipt names the exact SHA an
        # operator can temporarily restore; doctor must not rewrite history or
        # create that ref itself.
        DoctorCheck(
            id="integration.stranded_dependents",
            run=_check_stranded_dependents,
            owner=OWNER,
        ),
        # Report-only.  Both symptoms are durable publisher state, and the
        # repairs they call for — resolving a parked batch, cancelling one,
        # re-filing a repair — are the operator's judgement, not doctor's.
        # Clearing the diagnostic from here would only hide the stall: the
        # next tick rewrites it.
        DoctorCheck(
            id="integration.development_publisher_stalled",
            run=_check_publisher_stalled,
            owner=OWNER,
        ),
        # Fixable, and the fix is the same code the reconciliation tick and
        # ``integration_abort`` run — one implementation in
        # ``src.integration.delegate_release``.  It settles a ticket nothing
        # can schedule any more; it never releases a branch owner, a workspace
        # lock or a live writer, and it never manufactures a pass.
        DoctorCheck(
            id="integration.stranded_delegates",
            run=_check_stranded_delegates,
            fix=_fix_stranded_delegates,
            owner=OWNER,
        ),
        # Fixable, and the fix is the scheduler's own release: it frees only a
        # ``stale`` request -- one whose batch ended, is gone, or promoted
        # without its lease, with no unresolved write evidence -- and never
        # touches Git or a batch.  ``unsealed`` and ``blocked`` requests are
        # reported for ``aq integration clear-stale-request``.
        DoctorCheck(
            id="integration.stale_schedule",
            run=_check_stale_schedule,
            fix=_fix_stale_schedule,
            owner=OWNER,
        ),
        # Report-only.  The collector already records completion evidence
        # for a child whose published head proves out, so a child listed here
        # failed that proof or was held by a reviewer's verdict; advancing it
        # anyway is the supervisor's call (``aq integration redrive-child``).
        DoctorCheck(
            id="integration.stuck_children",
            run=_check_stuck_children,
            owner=OWNER,
        ),
        DoctorCheck(
            id="integration.blocked_collectors",
            run=_check_blocked_collectors,
            owner=OWNER,
        ),
        # Fixable, unlike ``integration.stranded_fences``, because it only
        # acts where that check's objection does not apply: the owning task
        # is finished or gone, no hierarchy/train project integrates the
        # repository (so no claim will ever take the branch), nothing live
        # names the task or the branch, and an attached writer's stop is
        # proven by the session provider, not read off a snapshot.  The fix
        # changes the ownership row only — never a checkout, workspace lock,
        # session or task.  ``timeout_s`` covers one transaction per row plus
        # one provider probe per attached row.
        DoctorCheck(
            id="integration.finished_branch_owners",
            run=_check_finished_branch_owners,
            fix=_fix_finished_branch_owners,
            owner=OWNER,
            timeout_s=60.0,
        ),
    ]


#: Snapshot for call-sites (tests, ad-hoc scripts) that want the list without
#: building a full :class:`~src.doctor.runner.DoctorRegistry`.
CHECKS = integration_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None, handler=None) -> CheckResult:
    """Run one integration check directly against *db* (no registry needed)."""
    check = _BY_ID[check_id]
    return await check.run(DoctorContext(config=config, db=db, handler=handler))
