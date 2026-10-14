"""Subject schedule, human hold and App trust diagnostics for the reconciler."""

from __future__ import annotations

import time

from sqlalchemy import select

from src.database.tables import integration_subjects
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "integration"
_DEVELOPMENT_APP_ITEMS = frozenset({"credential", "repository", "protection"})
_APP_MODE_SEVERITY = {"fail": Severity.ERROR, "warn": Severity.WARN}
CONTINUOUS_PROMOTION_LAG_SECONDS = 6 * 3600


def _unavailable(check_id, detail="database unavailable"):
    return CheckResult(check_id, Severity.INFO, detail)


def _read_error(check_id, detail, exc):
    return CheckResult(check_id, Severity.ERROR, detail,
                       data={"errors": [{"error": f"{type(exc).__name__}: {exc}"}]})


async def _check_delivery_engine(ctx: DoctorContext) -> CheckResult:
    from src.integration.delivery_engine import project_delivery_issues

    check_id = "integration.delivery_engine"
    if ctx.db is None:
        return _unavailable(check_id)
    issues = await project_delivery_issues(
        ctx.db, await ctx.db.list_projects(), ctx.config,
        orchestrator=getattr(ctx.handler, "orchestrator", None),
    )
    return CheckResult(
        check_id, Severity.ERROR if issues else Severity.OK,
        "\n".join(issue["detail"] for issue in issues) if issues
        else "every delivery mode has an engine and designated repository",
        data={"projects": issues},
    )


async def _check_branch_origins(ctx: DoctorContext) -> CheckResult:
    from src.integration.hierarchy import missing_origin_tasks_on

    check_id = "integration.branch_origins"
    if ctx.db is None:
        return _unavailable(check_id)
    findings = []
    for project in await ctx.db.list_projects():
        if project.hierarchical_integration_mode not in {"hierarchy", "train"}:
            continue
        if not project.integration_repository_id:
            continue  # integration.delivery_engine owns a missing repository.
        async with ctx.db._engine.connect() as conn:
            missing = await missing_origin_tasks_on(conn, project.id, project.integration_repository_id)
        if missing:
            findings.append({"project_id": project.id, "task_ids": missing,
                             "mode": project.hierarchical_integration_mode,
                             "generation": project.hierarchical_integration_generation})
    return CheckResult(
        check_id, Severity.ERROR if findings else Severity.OK,
        f"{sum(len(row['task_ids']) for row in findings)} task(s) lack live branch origins; "
        "repair with `aq doctor --check integration.branch_origins --fix`"
        if findings else "every managed task has a live branch origin",
        fixable=bool(findings), data={"projects": findings},
    )


async def _fix_branch_origins(ctx: DoctorContext) -> CheckResult:
    if ctx.handler is None:
        return _unavailable("integration.branch_origins", "command handler unavailable for Git proof")
    before = await _check_branch_origins(ctx)
    results = []
    for row in before.data.get("projects", []):
        results.append(await ctx.handler.execute("edit_project", {
            "project_id": row["project_id"], "hierarchical_integration_mode": row["mode"],
            "expected_integration_generation": row["generation"],
            "reason": "doctor backfill missing branch origins",
        }))
    after = await _check_branch_origins(ctx)
    after.fix_applied = bool(results) and after.severity == Severity.OK
    after.data["repairs"] = results
    return after


async def _subjects(ctx, *, held):
    check_id = "integration.subjects_held" if held else "integration.subjects_overdue"
    if ctx.db is None:
        return _unavailable(check_id)
    now = time.time()
    table = integration_subjects.c
    condition = table.next_due_at.is_(None) if held else table.next_due_at < now - 60
    async with ctx.db._engine.connect() as conn:
        result = await conn.execute(select(
            table.id, table.project_id, table.kind, table.phase, table.next_due_at,
            table.wait_reason, table.gate_id, table.due_set_at,
        ).where(table.phase != "done", condition).order_by(table.id))
        rows = [dict(row) for row in result.mappings()]
    return CheckResult(
        check_id, Severity.WARN if rows else Severity.OK,
        f"{len(rows)} subject(s) {'held for a human' if held else 'overdue for a visit'}",
        data={"subjects": rows, "observed_at": now},
    )


async def _check_subjects_overdue(ctx):
    return await _subjects(ctx, held=False)


async def _check_subjects_held(ctx):
    return await _subjects(ctx, held=True)


async def _check_legacy_deliveries(ctx: DoctorContext) -> CheckResult:
    """Expose historical SHA claims which today's default branch cannot reach."""
    from src.database.tables import integration_legacy_deliveries, repos
    from src.git.manager import GitError
    from src.integration.delivery_observer import DeliveryTarget

    check_id = "integration.legacy_deliveries"
    if ctx.db is None:
        return _unavailable(check_id)
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(select(integration_legacy_deliveries).order_by(
            integration_legacy_deliveries.c.repository_id,
            integration_legacy_deliveries.c.task_id,
        ))).mappings().all()
        repositories = {row["id"]: row for row in (await conn.execute(select(repos))).mappings()}
    observer = _doctor_observer(ctx, prerequisite=False) if rows else None
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


def _doctor_observer(ctx: DoctorContext, *, prerequisite=True):
    """The daemon's delivery observer, or a fresh one for an offline doctor run."""
    from src.git.github import GitHubAccess
    from src.git.manager import GitManager
    from src.integration.delivery_observer import DeliveryObserver, prerequisite_observer

    observer = (prerequisite_observer(ctx.db) if prerequisite else None)
    observer = observer or getattr(ctx.db, "_delivery_observer", None)
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
        return _unavailable(check_id)
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

    Spec §9.6: fail/warn items become ERROR/WARN; refused commands are ERROR.
    Only configured App-mode GitHub repositories count. Development judges
    credential, repository and protection, listing train anchors without judging
    them until cutover. This only reads GitHub through the App.
    """
    from src.projects.github import GitHubError, parse_github_repository

    check_id = "integration.trust"
    if ctx.db is None or ctx.handler is None:
        return _unavailable(check_id,
                            "App-mode readiness unavailable without database and command handler")
    integration = getattr(ctx.config, "integration", None)
    if getattr(integration, "github_app", None) is None:
        return _unavailable(check_id,
                            "App credential mode is not configured (integration.github_app)")
    try:
        projects = sorted(await ctx.db.list_projects(), key=lambda project: project.id)
    except Exception as exc:  # noqa: BLE001 - any read failure is the named finding
        return _read_error(check_id, "could not enumerate projects; check db.migrations", exc)

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
            result = await ctx.handler.execute("integration_app_verify", {"project_id": project.id})
        except Exception as exc:  # noqa: BLE001 - a crashed read is a finding, not a crash
            result = {"success": False, "outcome": "error",
                      "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict) or not result.get("success"):
            refusal = result if isinstance(result, dict) else {}
            code = str(refusal.get("outcome") or "invalid_result")
            entry = {"project_id": project.id, "mode": mode,
                     "code": code, "error": refusal.get("error")}
            if code == "policy_missing" and mode == "development":
                # The command needs a policy; a development project may have none yet.
                unverified.append(entry)
            else:
                findings.append({**entry, "item": "app_verify", "status": "fail",
                                 "codes": [code], "fix": refusal.get("error")})
            continue
        judged = _DEVELOPMENT_APP_ITEMS if mode == "development" else None
        items = []
        for item in result.get("items") or []:
            status = item.get("status")
            counted = judged is None or item.get("id") in judged
            codes = list(item.get("codes") or [])
            items.append({"id": item.get("id"), "status": status,
                          "codes": codes, "judged": counted})
            if counted and status in _APP_MODE_SEVERITY:
                findings.append({"project_id": project.id, "mode": mode, "item": item.get("id"),
                                 "status": status, "codes": codes, "fix": item.get("fix")})
        reports.append({"project_id": project.id, "mode": mode,
                        "ready": bool(result.get("ready")), "items": items})

    data = {"projects": reports, "findings": findings, "unverified": unverified}
    if findings:
        severity = Severity.ERROR if any(f["status"] == "fail" for f in findings) else Severity.WARN
        detail = "; ".join(
            f"{f['project_id']}: {f['item']} {f['status']} ({', '.join(f['codes'])})"
            for f in findings
        ) + "; run `aq integration app-verify PROJECT` for each fix"
    elif not reports and not unverified:
        severity = Severity.INFO
        detail = "no enabled GitHub project to verify for App credential mode"
    else:
        severity = Severity.OK if reports else Severity.INFO
        detail = f"{len(reports)} App-mode project(s) verified"
        if unverified:
            detail += "; not verified (development, no bound policy): " + ", ".join(
                entry["project_id"] for entry in unverified
            )
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
    check_id = "integration.finished_branch_owners"
    if ctx.db is None:
        return _unavailable(check_id, "database not initialised — branch ownership state unknown")
    from src.integration.finished_owners import finished_branch_owners

    findings = await finished_branch_owners(ctx.db, confirm_stopped=_stop_confirmer(ctx))
    releasable = [f for f in findings if f["blocker"] is None]
    kept = [f for f in findings if f["blocker"] is not None]
    data = {"count": len(releasable), "kept_count": len(kept),
            "releasable": releasable[:50], "kept": kept[:50]}
    if not findings:
        return CheckResult(check_id, Severity.OK,
                           "no branch owner row is held for a task that finished or is gone")
    if not releasable:
        first = kept[0]
        return CheckResult(
            check_id, Severity.INFO,
            f"{len(kept)} branch owner row(s) held for a finished or deleted task "
            f"are kept on purpose — e.g. {_describe_owner(first)}: {first['blocker']}", data=data)
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
    return CheckResult(check_id, Severity.WARN, detail, fixable=True, data=data)


async def _fix_finished_branch_owners(ctx: DoctorContext) -> CheckResult:
    """Release cleared owners under lock; repeats neither select them nor write more events."""
    from src.integration.finished_owners import release_finished_branch_owners

    released = await release_finished_branch_owners(
        ctx.db, confirm_stopped=_stop_confirmer(ctx), released_by="doctor"
    )
    return CheckResult(
        "integration.finished_branch_owners", Severity.OK,
        f"released {len(released)} branch owner row(s) held for a finished or deleted "
        "task; each is recorded as an integration.branch_owner_released event",
        fixable=True, fix_applied=True,
        data={"count": len(released), "released": released[:50]},
    )


async def _check_promotion_flow(ctx: DoctorContext) -> CheckResult:
    """Revalidate stored flows (layers 1-3, R17, spec §3.11), naming the first failure.

    Invalid flows leave targets ``misconfigured`` and the train promoting to
    the default branch. Read-only: flows are operator configuration.
    """
    from src.integration.promotion_steps import recheck_stored_flows

    check_id = "integration.promotion_flow"
    if ctx.db is None or ctx.handler is None:
        detail = "promotion flow re-validation unavailable without database and command handler"
        return _unavailable(check_id, detail)
    try:
        found = await recheck_stored_flows(
            ctx.db,
            lambda pid: ctx.handler.execute("promote_validate", {"project_id": pid}),
        )
    except Exception as exc:  # noqa: BLE001 - any read failure is the named finding
        return _read_error(check_id, "could not read stored promotion flows; check db.migrations", exc)
    if not found:
        return CheckResult(check_id, Severity.OK, "every stored promotion flow validates")
    projects = [
        {"project_id": project_id, "problems": problems,
         **{key: problems[0].get(key) for key in ("code", "pointer", "message")}}
        for project_id, problems in sorted(found.items())
    ]
    named = "; ".join(f"{p['project_id']}: {p['code']} at {p['pointer']!r}" for p in projects)
    return CheckResult(check_id, Severity.ERROR,
        f"{len(projects)} stored promotion flow(s) no longer validate; their targets are "
        f"misconfigured and the train promotes to the default branch ({named}). Fix the "
        "flow with `aq promote validate --file` and `aq project set <id> promotion-flow`.",
        data={"projects": projects},
    )


async def continuous_promotion_lag(
    ctx: DoctorContext, *, project_ids=None, now=None,
    max_age_seconds=CONTINUOUS_PROMOTION_LAG_SECONDS,
) -> dict:
    """Age the oldest source-only first-parent commit from one fetched repo snapshot."""
    from src.database.tables import projects, repos
    from src.git.manager import is_valid_git_oid
    from src.integration.delivery_observer import DeliveryTarget
    from src.integration.promotion_steps import FlowSchema

    report = {"findings": [], "unavailable": [], "checked": 0,
              "max_age_seconds": max_age_seconds}
    if ctx.db is None or not getattr(ctx.db, "_engine", None):
        return report
    now = time.time() if now is None else now
    statement = select(
        projects.c.id, projects.c.promotion_flow, repos.c.id.label("repository_id"),
        repos.c.url, repos.c.default_branch,
    ).select_from(projects.join(repos, (repos.c.id == projects.c.integration_repository_id)
                               & (repos.c.project_id == projects.c.id))).where(
        projects.c.status == "ACTIVE",
        projects.c.hierarchical_integration_mode.in_(("train", "hierarchy", "development")),
        projects.c.promotion_flow.is_not(None),
    )
    if project_ids is not None:
        statement = statement.where(projects.c.id.in_(project_ids))
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(statement.order_by(projects.c.id))).mappings().all()
    observer = None
    for row in rows:
        flow = FlowSchema.validate(row["promotion_flow"], default_branch=row["default_branch"])
        # Trust is checked separately; branch drift needs only flow layers 1-2.
        if any(problem.layer < 3 for problem in flow.problems):
            continue
        steps = [step for step in flow.flow or [] if step["type"] == "continuous"]
        if not steps:
            continue
        observer = observer or _doctor_observer(ctx, prerequisite=False)
        try:
            snapshot = await observer.snapshot(DeliveryTarget(
                row["id"], row["repository_id"], row["url"], "refs/heads/" + row["default_branch"],
            ))
            observed = getattr(snapshot, "observation", snapshot)
            if observed.error:
                raise ValueError(observed.error)
        except Exception as exc:  # a failed read is visible, never a healthy flow
            report["unavailable"].append({"project_id": row["id"],
                "repository_id": row["repository_id"], "reason": str(exc)})
            continue
        for step in steps:
            identity = {"project_id": row["id"], "repository_id": row["repository_id"],
                        "step_id": step["id"], "source": step["source"], "target": step["target"]}
            try:
                source = observed.source_heads.get("refs/remotes/origin/" + step["source"])
                target = observed.source_heads.get("refs/remotes/origin/" + step["target"])
                if not is_valid_git_oid(source) or not is_valid_git_oid(target):
                    raise ValueError("source or target branch head unavailable")
                counts = await observed.git.arun_git_result([
                    "--no-replace-objects", "rev-list", "--left-right", "--count",
                    f"{target}...{source}",
                ], cwd=observed.store, timeout=5)
                if counts.returncode:
                    raise ValueError("branch commit counts unavailable")
                behind, ahead = map(int, counts.stdout.split())
                report["checked"] += 1
                if not ahead:
                    continue
                dates = await observed.git.arun_git_result([
                    "--no-replace-objects", "log", "--first-parent", "--format=%ct",
                    f"{target}..{source}",
                ], cwd=observed.store, timeout=5)
                if dates.returncode or not dates.stdout.strip():
                    raise ValueError("source-only commit dates unavailable")
                oldest = min(int(value) for value in dates.stdout.split())
                age = now - oldest
                if age <= max_age_seconds:
                    continue
                report["findings"].append({**identity, "source_sha": source, "target_sha": target,
                    "ahead": ahead, "behind": behind, "oldest_unpromoted_at": oldest,
                    "age_seconds": age, "max_age_seconds": max_age_seconds,
                    "detail": f"continuous step {step['id']} ({step['source']} -> {step['target']}) "
                              f"has {ahead} source-only commit(s), oldest {age / 3600:.1f}h "
                              f"(threshold {max_age_seconds / 3600:g}h); inspect promotion status, "
                              "source CI and the project promotion-continuous activation"})
            except Exception as exc:  # one unreadable step does not hide the others
                report["unavailable"].append({**identity, "reason": str(exc)})
    return report


async def _check_continuous_promotion_lag(ctx: DoctorContext) -> CheckResult:
    check_id = "integration.continuous_promotion_lag"
    if ctx.db is None:
        return _unavailable(check_id)
    try:
        report = await continuous_promotion_lag(ctx)
    except Exception as exc:
        return _read_error(check_id, "could not observe continuous promotion branches", exc)
    findings, unavailable = report["findings"], report["unavailable"]
    return CheckResult(check_id, Severity.WARN if findings or unavailable else Severity.OK,
        f"{len(findings)} continuous promotion step(s) have work older than "
        f"{report['max_age_seconds'] / 3600:g}h; {len(unavailable)} unavailable"
        + "".join(f"\n{item['project_id']}: {item['detail']}" for item in findings),
        data=report)


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
        return _unavailable(check_id, "CI source report unavailable without database")
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


async def _check_promotion_stranded(ctx: DoctorContext) -> CheckResult:
    """Open promotion intents whose task StepAdmission refuses idle on every train visit."""
    from src.database.tables import integration_batch_members, integration_batches, tasks
    from src.integration.promotion_steps import promotion_task_defect

    check_id = "integration.promotion_stranded"
    if ctx.db is None:
        return _unavailable(check_id)
    batch, member = integration_batches.c, integration_batch_members.c
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(select(
            batch.id.label("batch_id"), batch.project_id, batch.request_id, tasks.c.id,
            tasks.c.task_type, tasks.c.status, tasks.c.profile_id,
        ).select_from(integration_batches.join(
            integration_batch_members, member.batch_id == batch.id,
        ).join(tasks, tasks.c.id == member.task_id)).where(
            batch.trigger == "promotion", batch.target_ref.is_not(None), batch.intent != "aborted",
            batch.lifecycle.not_in(("promoted", "aborted", "failed", "empty")),
        ).order_by(batch.id))).mappings().all()
    stranded = [{"batch_id": row["batch_id"], "project_id": row["project_id"],
                 "request_id": row["request_id"], "task_id": row["id"], "reason": reason}
                for row in rows if (reason := promotion_task_defect(row, allow_completed=True))]
    return CheckResult(check_id, Severity.ERROR if stranded else Severity.OK, "; ".join(
        f"batch {entry['batch_id']}: {entry['reason']}; back up and restore it with `aq promote "
        f"repair {entry['request_id']} --project {entry['project_id']}`" for entry in stranded)
        or "every open promotion intent has an admissible task", data={"stranded": stranded})


async def stalled_train_batches(ctx: DoctorContext, *, now=None, project_ids=None) -> list[dict]:
    """Age alarms for sealed inputs, independent of the legacy Subject journal."""
    from sqlalchemy import union_all

    from src.database.tables import archived_tasks, integration_batches, projects, tasks
    from src.integration.models import IntegrationTrainPolicy
    from src.integration.repair import ordinary_repair_id

    now = time.time() if now is None else now
    batch = integration_batches.c
    statement = select(
        batch.id.label("batch_id"), batch.project_id, batch.repository_id, batch.target_ref,
        batch.updated_at, batch.repair_attempt_count, projects.c.hierarchical_integration_policy,
    ).select_from(integration_batches.join(projects, projects.c.id == batch.project_id)).where(
        batch.target_ref.is_not(None), batch.intent == "open", batch.lifecycle == "sealed",
        projects.c.status == "ACTIVE",
        projects.c.hierarchical_integration_mode.in_(("development", "hierarchy", "train")),
    ).order_by(batch.id)
    if project_ids is not None:
        statement = statement.where(batch.project_id.in_(project_ids))
    async with ctx.db._engine.connect() as conn:
        rows = (await conn.execute(statement)).mappings().all()
        ids = [ordinary_repair_id(row["batch_id"], row["repair_attempt_count"])
               for row in rows if row["repair_attempt_count"]]
        repairs = {}
        if ids:
            # Prefer the live row if archival overlaps a read.
            repairs = {row["id"]: row for row in (await conn.execute(union_all(*(
                select(table.c.id, table.c.status, table.c.updated_at).where(table.c.id.in_(ids))
                for table in (archived_tasks, tasks)
            )))).mappings().all()}
    train = getattr(getattr(ctx.handler, "orchestrator", None), "integration_train", None)
    lanes = {(row["project_id"], row["repository_id"], row["target_ref"]): row
             for row in train.status()} if train is not None else {}
    findings = []
    for row in rows:
        repair_id = (ordinary_repair_id(row["batch_id"], row["repair_attempt_count"])
                     if row["repair_attempt_count"] else None)
        repair = repairs.get(repair_id)
        if repair is not None and repair["status"] not in {"COMPLETED", "FAILED", "BLOCKED"}:
            continue  # Existing task/repair capacity diagnostics own this wait.
        lane = lanes.get((row["project_id"], row["repository_id"], row["target_ref"]), {})
        progress_at = max(row["updated_at"], repair["updated_at"] if repair else 0)
        if lane.get("batch_id") == row["batch_id"]:
            progress_at = max(progress_at, lane.get("progress_at", 0))
        policy = IntegrationTrainPolicy.model_validate(
            (row["hierarchical_integration_policy"] or {}).get("train") or {})
        due_at = progress_at + policy.settling_cap_seconds
        due_at = max(due_at, lane.get("deferred_until", 0) + 60)
        if lane.get("running"):
            due_at = max(due_at, lane.get("visit_started_at", 0)
                         + train.visit_timeout_seconds + 60)
        if now <= due_at:
            continue
        findings.append({
            "batch_id": row["batch_id"], "project_id": row["project_id"],
            "repository_id": row["repository_id"], "target_ref": row["target_ref"],
            "repair_task_id": repair_id, "repair_status": repair["status"] if repair else None,
            "progress_at": progress_at, "due_at": due_at,
            "age_seconds": now - progress_at, "observation": lane,
            "detail": f"sealed batch {row['batch_id']} has no progress for "
                      f"{int((now - progress_at) / 60)}m past its visit deadline; inspect "
                      f"aq integration status {row['project_id']} and its candidate checks",
        })
    return findings


async def _check_train_batches_stalled(ctx: DoctorContext) -> CheckResult:
    check_id = "integration.train_batches_stalled"
    if ctx.db is None:
        return _unavailable(check_id)
    findings = await stalled_train_batches(ctx)
    return CheckResult(check_id, Severity.WARN if findings else Severity.OK,
                       "; ".join(row["detail"] for row in findings)
                       or "no sealed train batch is past its progress deadline",
                       data={"findings": findings})


def integration_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=f"integration.{name}", run=run, fix=fix, timeout_s=timeout, owner=OWNER)
        for name, run, timeout, fix in (
            ("delivery_engine", _check_delivery_engine, 30.0, None),
            ("branch_origins", _check_branch_origins, 300.0, _fix_branch_origins),
            ("legacy_deliveries", _check_legacy_deliveries, 300.0, None),
            ("completed_undelivered", _check_completed_undelivered, 300.0, None),
            ("finished_branch_owners", _check_finished_branch_owners,
             30.0, _fix_finished_branch_owners),
            ("subjects_overdue", _check_subjects_overdue, 5.0, None),
            ("subjects_held", _check_subjects_held, 5.0, None),
            ("trust", _check_trust, 60.0, None),
            ("promotion_flow", _check_promotion_flow, 60.0, None),
            ("continuous_promotion_lag", _check_continuous_promotion_lag, 60.0, None),
            ("promotion_stranded", _check_promotion_stranded, 30.0, None),
            ("train_batches_stalled", _check_train_batches_stalled, 30.0, None),
            ("ci_source", _check_ci_source, 30.0, None),
        )
    ]


CHECKS = integration_checks()
_BY_ID = {check.id: check for check in CHECKS}


async def run_check(db, check_id, *, config=None, handler=None):
    return await _BY_ID[check_id].run(
        DoctorContext(config=config, db=db, handler=handler)
    )
