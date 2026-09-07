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
    ]


#: Snapshot for call-sites (tests, ad-hoc scripts) that want the list without
#: building a full :class:`~src.doctor.runner.DoctorRegistry`.
CHECKS = integration_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None, handler=None) -> CheckResult:
    """Run one integration check directly against *db* (no registry needed)."""
    check = _BY_ID[check_id]
    return await check.run(DoctorContext(config=config, db=db, handler=handler))
