"""Subject schedule, human hold and App trust diagnostics for the reconciler."""

from __future__ import annotations

import time
from sqlalchemy import select
from src.database.tables import integration_subjects
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "integration"
_DEVELOPMENT_APP_ITEMS = frozenset({"credential", "repository", "protection"})
_APP_MODE_SEVERITY = {"fail": Severity.ERROR, "warn": Severity.WARN}


async def _subjects(ctx, *, held):
    check_id = "integration.subjects_held" if held else "integration.subjects_overdue"
    if ctx.db is None:
        return CheckResult(
            id=check_id, severity=Severity.INFO, detail="database unavailable"
        )
    now = time.time()
    table = integration_subjects.c
    condition = table.next_due_at.is_(None) if held else table.next_due_at < now - 60
    async with ctx.db._engine.connect() as conn:
        rows = [
            dict(row)
            for row in (
                await conn.execute(
                    select(
                        table.id,
                        table.project_id,
                        table.kind,
                        table.phase,
                        table.next_due_at,
                        table.wait_reason,
                        table.gate_id,
                        table.due_set_at,
                    )
                    .where(table.phase != "done", condition)
                    .order_by(table.id)
                )
            ).mappings()
        ]
    return CheckResult(
        id=check_id,
        severity=Severity.WARN if rows else Severity.OK,
        detail=f"{len(rows)} subject(s) {'held for a human' if held else 'overdue for a visit'}",
        data={"subjects": rows, "observed_at": now},
    )


async def _check_subjects_overdue(ctx):
    return await _subjects(ctx, held=False)


async def _check_subjects_held(ctx):
    return await _subjects(ctx, held=True)


async def _check_trust(ctx: DoctorContext) -> CheckResult:
    """Run ``aq integration app-verify`` for every App-mode project not disabled.

    App-mode integration train spec §9.6: each ``fail`` item is an ERROR and each
    ``warn`` item a WARN.  A project counts when the install configures
    ``integration.github_app`` and its repository is on github.com; a local
    remote never uses the App.  A development project is judged on the items
    its publisher needs (credential, repository, protection); the train's
    anchors are listed but not judged until the cutover.  A project the command
    refuses (no binding, no designated repository) is an ERROR: App mode cannot
    run it.  Read-only: the command only reads GitHub through the App.
    """
    from src.projects.github import GitHubError, parse_github_repository

    check_id = "integration.trust"
    if ctx.db is None or ctx.handler is None:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="App-mode readiness unavailable without database and command handler",
        )
    integration = getattr(ctx.config, "integration", None)
    if getattr(integration, "github_app", None) is None:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="App credential mode is not configured (integration.github_app)",
        )
    try:
        projects = sorted(await ctx.db.list_projects(), key=lambda project: project.id)
    except Exception as exc:  # noqa: BLE001 - any read failure is the named finding
        return CheckResult(
            id=check_id,
            severity=Severity.ERROR,
            detail="could not enumerate projects; check db.migrations",
            data={"errors": [{"error": f"{type(exc).__name__}: {exc}"}]},
        )

    reports: list[dict] = []
    findings: list[dict] = []
    unverified: list[dict] = []
    for project in projects:
        mode = getattr(project, "hierarchical_integration_mode", None) or "disabled"
        if mode == "disabled":
            continue
        repository_id = getattr(project, "integration_repository_id", None)
        repository = await ctx.db.get_repo(repository_id) if repository_id else None
        url = getattr(repository, "url", None) or getattr(project, "repo_url", None)
        try:
            parse_github_repository(url or "")
        except GitHubError:
            continue
        try:
            result = await ctx.handler.execute(
                "integration_app_verify", {"project_id": project.id}
            )
        except Exception as exc:  # noqa: BLE001 - a crashed read is a finding, not a crash
            result = {
                "success": False,
                "outcome": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if not isinstance(result, dict) or not result.get("success"):
            refusal = result if isinstance(result, dict) else {}
            code = str(refusal.get("outcome") or "invalid_result")
            entry = {
                "project_id": project.id,
                "mode": mode,
                "code": code,
                "error": refusal.get("error"),
            }
            if code == "policy_missing" and mode == "development":
                # The command needs a policy; a development project may have none yet.
                unverified.append(entry)
            else:
                findings.append(
                    {
                        **entry,
                        "item": "app_verify",
                        "status": "fail",
                        "codes": [code],
                        "fix": refusal.get("error"),
                    }
                )
            continue
        judged = _DEVELOPMENT_APP_ITEMS if mode == "development" else None
        items = []
        for item in result.get("items") or []:
            status = item.get("status")
            counted = judged is None or item.get("id") in judged
            items.append(
                {
                    "id": item.get("id"),
                    "status": status,
                    "codes": list(item.get("codes") or []),
                    "judged": counted,
                }
            )
            if counted and status in _APP_MODE_SEVERITY:
                findings.append(
                    {
                        "project_id": project.id,
                        "mode": mode,
                        "item": item.get("id"),
                        "status": status,
                        "codes": list(item.get("codes") or []),
                        "fix": item.get("fix"),
                    }
                )
        reports.append(
            {
                "project_id": project.id,
                "mode": mode,
                "ready": bool(result.get("ready")),
                "items": items,
            }
        )

    data = {"projects": reports, "findings": findings, "unverified": unverified}
    if findings:
        severity = (
            Severity.ERROR
            if any(f["status"] == "fail" for f in findings)
            else Severity.WARN
        )
        return CheckResult(
            id=check_id,
            severity=severity,
            detail="; ".join(
                f"{f['project_id']}: {f['item']} {f['status']} ({', '.join(f['codes'])})"
                for f in findings
            )
            + "; run `aq integration app-verify PROJECT` for each fix",
            data=data,
        )
    if not reports and not unverified:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="no enabled GitHub project to verify for App credential mode",
            data=data,
        )
    detail = f"{len(reports)} App-mode project(s) verified"
    if unverified:
        detail += "; not verified (development, no bound policy): " + ", ".join(
            entry["project_id"] for entry in unverified
        )
    severity = Severity.OK if reports else Severity.INFO
    return CheckResult(id=check_id, severity=severity, detail=detail, data=data)



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


async def _check_promotion_flow(ctx: DoctorContext) -> CheckResult:
    """Re-run layers 1-3 on every stored promotion flow (R17, spec §3.11).

    A flow that stopped validating (a manifest change, a default-branch rename,
    a tightened schema) leaves its targets ``misconfigured`` and the train
    promoting to the default branch; this names the first failing pointer per
    project. Read-only: the flow is operator configuration, so nothing is
    fixed here.
    """
    from src.integration.promotion_steps import recheck_stored_flows

    check_id = "integration.promotion_flow"
    if ctx.db is None or ctx.handler is None:
        return CheckResult(
            id=check_id,
            severity=Severity.INFO,
            detail="promotion flow re-validation unavailable without database and command handler",
        )
    handler = ctx.handler
    try:
        found = await recheck_stored_flows(
            ctx.db,
            lambda project_id: handler.execute("promote_validate", {"project_id": project_id}),
        )
    except Exception as exc:  # noqa: BLE001 - any read failure is the named finding
        return CheckResult(
            id=check_id,
            severity=Severity.ERROR,
            detail="could not read stored promotion flows; check db.migrations",
            data={"errors": [{"error": f"{type(exc).__name__}: {exc}"}]},
        )
    if not found:
        return CheckResult(
            id=check_id, severity=Severity.OK, detail="every stored promotion flow validates"
        )
    projects = [
        {
            "project_id": project_id,
            "code": problems[0].get("code"),
            "pointer": problems[0].get("pointer"),
            "message": problems[0].get("message"),
            "problems": problems,
        }
        for project_id, problems in sorted(found.items())
    ]
    named = "; ".join(
        f"{entry['project_id']}: {entry['code']} at {entry['pointer']!r}" for entry in projects
    )
    return CheckResult(
        id=check_id,
        severity=Severity.ERROR,
        detail=(
            f"{len(projects)} stored promotion flow(s) no longer validate; their targets are "
            f"misconfigured and the train promotes to the default branch ({named}). Fix the "
            "flow with `aq promote validate --file` and `aq project set <id> promotion-flow`."
        ),
        data={"projects": projects},
    )


def integration_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="integration.finished_branch_owners", run=_check_finished_branch_owners,
            fix=_fix_finished_branch_owners, owner=OWNER, timeout_s=30.0,
        ),
        DoctorCheck(
            id="integration.subjects_overdue", run=_check_subjects_overdue, owner=OWNER
        ),
        DoctorCheck(
            id="integration.subjects_held", run=_check_subjects_held, owner=OWNER
        ),
        DoctorCheck(
            id="integration.trust", run=_check_trust, owner=OWNER, timeout_s=60.0
        ),
        DoctorCheck(
            id="integration.promotion_flow", run=_check_promotion_flow, owner=OWNER,
            timeout_s=60.0,
        ),
    ]


CHECKS = integration_checks()
_BY_ID = {check.id: check for check in CHECKS}


async def run_check(db, check_id, *, config=None, handler=None):
    return await _BY_ID[check_id].run(
        DoctorContext(config=config, db=db, handler=handler)
    )
