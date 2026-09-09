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

import time

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.models import TaskStatus
from src.review_keys import review_task_dedup_key

OWNER = "integration"

#: How far back to look.  Long enough that a real stall is caught the same
#: working session, short enough that historical debt (PRs deliberately left
#: open, work from before this check existed) does not permanently redden the
#: report.
_WINDOW_SECONDS = 24 * 60 * 60

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
        checkout = await ctx.db.get_project_workspace_path(project_id)
        if not checkout:
            return None
        merged = await git.acheck_pr_merged(checkout, pr_url)
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
            "branch": f"aq/{row['task_id']}",
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
            f"{len(parked)} branch discard(s) did not finish — e.g. {first['branch']}: "
            f"{first['error']}. The task is already deleted; the ref is still on the "
            "remote. Delete it by hand, or re-arm the discard with "
            "`aq doctor --check integration.branch_discards --fix`"
        ),
        fixable=True,
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
                    .where(sessions.c.task_id == row["owner_id"], sessions.c.state != "stopped")
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
            "'canonical branch is not reserved by this task'. Report only: recovering an "
            "ownership row needs proof this check cannot take (the writer's provider stopped, "
            "its checkout clean and published), so the repair belongs to the guarded "
            "integration recovery path, not to doctor"
        ),
        fixable=False,
        data={"count": len(stranded), "fences": stranded},
    )


def integration_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="integration.operational",
            run=_check_operational,
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
        # Report-only, deliberately.  Everything doctor can see about a
        # stranded row is a database snapshot, and the database is not where
        # the danger is: an owner row can look dead while the writer's
        # provider is still running against the checkout, or while the
        # checkout holds work no remote has.  Returning the row to
        # ``reserved`` from here would hand the branch to the next claim on a
        # snapshot alone, and would race any guarded rebind that touches the
        # attachment without changing the owner fields a CAS could see.  The
        # write belongs to the integration recovery path, which takes the
        # proofs doctor cannot.
        DoctorCheck(
            id="integration.stranded_fences",
            run=_check_stranded_fences,
            owner=OWNER,
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
