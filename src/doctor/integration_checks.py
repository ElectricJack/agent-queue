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


async def _check_legacy_deliveries(ctx: DoctorContext) -> CheckResult:
    """Expose historical SHA claims which today's default branch cannot reach."""
    from src.database.tables import integration_legacy_deliveries, repos
    from src.git.github import GitHubAccess
    from src.git.manager import GitError, GitManager
    from src.integration.delivery_observer import DeliveryObserver, DeliveryTarget

    check_id = "integration.legacy_deliveries"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_legacy_deliveries).order_by(
            integration_legacy_deliveries.c.repository_id,
            integration_legacy_deliveries.c.task_id,
        ))).mappings().all()
        repositories = {row["id"]: row for row in (await conn.execute(select(repos))).mappings()}
    observer = getattr(ctx.db, "_delivery_observer", None)
    if observer is None and rows:
        integration = getattr(ctx.config, "integration", None)
        observer = DeliveryObserver(ctx.db, git=GitManager(GitHubAccess.from_config(
            getattr(integration, "github_app", None))), data_dir=ctx.config.data_dir)
    snapshots, findings, unknown = {}, [], []
    for row in rows:
        repo = repositories.get(row["repository_id"])
        identity = {"task_id": row["task_id"], "project_id": row["project_id"],
                    "repository_id": row["repository_id"], "proof": row["proof"],
                    "sha": row["delivered_sha"] or row["target_sha"]}
        try:
            if repo is None or not repo["url"]:
                raise ValueError("repository unavailable")
            if repo["id"] not in snapshots:
                snapshots[repo["id"]] = await observer.snapshot(DeliveryTarget(
                    row["project_id"], repo["id"], repo["url"],
                    "refs/heads/" + repo["default_branch"].removeprefix("refs/heads/")))
            snapshot = snapshots[repo["id"]]
            observation = getattr(snapshot, "observation", snapshot)
            if observation.error or not observation.target_oid:
                raise ValueError(observation.error or "default branch unavailable")
            reachable = await observer.git.ais_ancestor(
                observation.store, identity["sha"], observation.target_oid, strict=True)
            if reachable is None:
                raise ValueError("SHA reachability unavailable")
            if reachable is False:
                findings.append({**identity, "target_oid": observation.target_oid})
        except (GitError, OSError, ValueError) as exc:
            unknown.append({**identity, "reason": str(exc)})
    return CheckResult(id=check_id,
        severity=Severity.WARN if findings or unknown else Severity.OK,
        detail=f"{len(findings)} legacy delivery SHA(s) not reachable from the default branch; "
               f"{len(unknown)} unknown",
        data={"unreachable": findings, "unknown": unknown, "checked": len(rows)})


def _doctor_observer(ctx: DoctorContext):
    """The daemon's delivery observer, or a fresh one for an offline doctor run."""
    from src.git.github import GitHubAccess
    from src.git.manager import GitManager
    from src.integration.delivery_observer import DeliveryObserver, prerequisite_observer

    observer = prerequisite_observer(ctx.db) or getattr(ctx.db, "_delivery_observer", None)
    if observer is None:
        integration = getattr(ctx.config, "integration", None)
        observer = DeliveryObserver(ctx.db, git=GitManager(GitHubAccess.from_config(
            getattr(integration, "github_app", None))), data_dir=ctx.config.data_dir)
    return observer


async def completed_undelivered(ctx: DoctorContext, *, project_ids=None, now=None) -> dict:
    """Why each COMPLETED task branch the default branch cannot reach is still on origin.

    :func:`observe_completed_branches` over the daemon's observer; doctor and
    the stall sweep share this read.
    """
    from src.integration.delivery_branches import observe_completed_branches

    return await observe_completed_branches(
        ctx.db, _doctor_observer(ctx), project_ids=project_ids, now=now,
    )


def _describe_completed(entry: dict) -> str:
    archived = " (archived)" if entry["archived"] else ""
    return f"{entry['task_id']}{archived} on {entry['branch']}: {entry['reason']}"


async def _check_completed_undelivered(ctx: DoctorContext) -> CheckResult:
    """List COMPLETED tasks whose branch holds work the default branch cannot reach."""
    check_id = "integration.completed_undelivered"
    if ctx.db is None:
        return CheckResult(id=check_id, severity=Severity.INFO, detail="database unavailable")
    inventory = await completed_undelivered(ctx)
    entries = inventory["entries"]
    stranded = [e for e in entries if not e["accounted"] and e["rule"] != "unknown"]
    accounted = [e for e in entries if e["accounted"]]
    unknown = [e for e in entries if e["rule"] == "unknown"] + inventory["unavailable"]
    by_rule: dict[str, int] = {}
    for entry in entries:
        by_rule[entry["rule"]] = by_rule.get(entry["rule"], 0) + 1
    data = {
        "stranded": stranded[:100], "accounted": accounted[:100], "unknown": unknown[:100],
        "by_rule": dict(sorted(by_rule.items())), "projects": inventory["projects"],
    }
    if stranded:
        return CheckResult(
            id=check_id, severity=Severity.WARN,
            detail=(
                f"{len(stranded)} COMPLETED task branch(es) hold work the default branch "
                "cannot reach, and nothing on record will deliver or retire them — e.g. "
                f"{_describe_completed(stranded[0])}. Settle it: {stranded[0]['remedy']}"
            ),
            data=data,
        )
    detail = "no COMPLETED task branch holds work that nothing will deliver or retire"
    if accounted:
        rules = ", ".join(
            f"{count} {rule}" for rule, count in data["by_rule"].items() if rule != "unknown"
        )
        detail += f"; {len(accounted)} not yet on the default branch are accounted for ({rules})"
    if unknown:
        detail += f"; {len(unknown)} could not be read"
    return CheckResult(
        id=check_id, severity=Severity.INFO if unknown else Severity.OK, detail=detail, data=data,
    )


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


async def _check_ci_source(ctx: DoctorContext) -> CheckResult:
    """Name the runner producing each project's required checks, per target kind.

    A stored ``ci`` block that no longer validates is an error: the train's
    lanes read it as unset and gate every target on hosted checks. Read-only:
    the policy is operator configuration, written with
    ``aq project set <id> integration-policy``.
    """
    from pydantic import ValidationError

    from src.database.tables import projects
    from src.integration.models import HierarchicalIntegrationPolicy, integration_ci_sources

    check_id = "integration.ci_source"
    if ctx.db is None:
        return CheckResult(
            id=check_id, severity=Severity.INFO,
            detail="CI source report unavailable without database",
        )
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(select(
            projects.c.id, projects.c.hierarchical_integration_policy,
        ).where(projects.c.hierarchical_integration_policy.is_not(None))
         .order_by(projects.c.id))).all()
    reported, invalid, rollback = [], [], []
    for project_id, policy in rows:
        if isinstance(policy, dict) and policy.get("ci") is not None:
            rollback.append(project_id)
        sources = integration_ci_sources(policy)
        if isinstance(policy, dict) and policy.get("ci") is not None \
                and sources["origin"] == "default":
            try:
                HierarchicalIntegrationPolicy.model_validate(policy)
                error = "ci block is not an object"
            except ValidationError as exc:
                first = exc.errors()[0]
                where = ".".join(str(part) for part in first["loc"])
                error = f"{where}: {first['msg']}" if where else first["msg"]
            except (TypeError, ValueError) as exc:
                error = str(exc).splitlines()[0]
            invalid.append({"project_id": project_id, "error": error})
        if sources["origin"] != "default":
            reported.append({"project_id": project_id, **sources})
    rollback_note = (
        " Rollback hazard: older daemons reject stored ci as an unknown policy field. "
        "Before downgrading, have the operator remove the ci block with "
        "`aq project set <id> integration-policy` for: " + ", ".join(rollback) + "."
        if rollback else ""
    )
    if invalid:
        named = "; ".join(f"{entry['project_id']}: {entry['error']}" for entry in invalid)
        return CheckResult(
            id=check_id, severity=Severity.ERROR,
            detail=(
                f"{len(invalid)} stored ci block(s) no longer validate, so their trains "
                f"read hosted checks everywhere ({named}). Fix the policy with "
                "`aq project set <id> integration-policy`." + rollback_note
            ),
            data={"projects": reported, "invalid": invalid, "rollback_projects": rollback},
        )
    local = [entry for entry in reported if "local" in entry.values()
             or "hybrid" in entry.values()]
    return CheckResult(
        id=check_id, severity=Severity.OK,
        detail=(f"{len(local)} project(s) run required checks locally; every other target "
                "reads hosted checks" if local else "every project reads hosted checks")
               + rollback_note,
        data={"projects": reported, "rollback_projects": rollback},
    )


def integration_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id="integration.legacy_deliveries", run=_check_legacy_deliveries,
                    owner=OWNER, timeout_s=300.0),
        DoctorCheck(id="integration.completed_undelivered", run=_check_completed_undelivered,
                    owner=OWNER, timeout_s=300.0),
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
        DoctorCheck(
            id="integration.ci_source", run=_check_ci_source, owner=OWNER, timeout_s=30.0
        ),
    ]


CHECKS = integration_checks()
_BY_ID = {check.id: check for check in CHECKS}


async def run_check(db, check_id, *, config=None, handler=None):
    return await _BY_ID[check_id].run(
        DoctorContext(config=config, db=db, handler=handler)
    )
