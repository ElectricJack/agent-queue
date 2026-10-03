"""The reconciler-era integration doctor checks.

Integration-train simplification §5.5: twenty checks become three.

* ``integration.subjects_overdue`` — a subject the reconciler engine owns is
  past ``next_due_at`` by more than one visit interval: nothing is visiting it.
* ``integration.subjects_held`` — subjects waiting on a person, with how long:
  an open gate (``aq integration gate answer``) or an operator hold
  (``aq integration hold --release``).
* ``integration.trust`` — the App-mode anchors (credential, repository,
  protection, manifest), read through the App.

Everything else the older ``integration.*`` checks reported is an observer fact
``aq integration status`` shows or a state the reconciler cannot reach; those
checks stay registered until their removal gate in
:data:`src.commands.integration_legacy.LEGACY_INTEGRATION_DOCTOR_CHECKS` holds.
All three are report-only.
"""

from __future__ import annotations

import json
import time

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "integration"
OVERDUE = "integration.subjects_overdue"
HELD = "integration.subjects_held"
TRUST = "integration.trust"

#: One reconciler visit interval.  The integration service ticks every five
#: seconds and one remote pass may spend a thirty-second call budget, so a due
#: subject is visited well inside a minute on a live daemon; past that the
#: reconciler is stopped, failing or starved.
OVERDUE_AFTER_SECONDS = 60.0

#: A decision waiting longer than a working day is reported as a warning: the
#: subject it holds blocks every later member of its train.
STALE_HOLD_SECONDS = 24 * 60 * 60

#: Rows listed in ``data``; the counts stay exact.
_LISTED = 50


#: What development mode depends on under App credentials: the App's token, the
#: repository it names, and a protection the publisher's unattested push gets
#: through (App-mode spec §8.2).  The manifest, variables and audit serve the
#: train, and stay unset on purpose until its cutover.
_DEVELOPMENT_APP_ITEMS = frozenset({"credential", "repository", "protection"})
_APP_MODE_SEVERITY = {"fail": Severity.ERROR, "warn": Severity.WARN}


async def app_mode_readiness(ctx: DoctorContext, *, check_id: str = TRUST) -> CheckResult:
    """Run ``aq integration legacy app-verify`` for every App-mode project not disabled.

    App-mode integration train spec §9.6: each ``fail`` item is an ERROR and each
    ``warn`` item a WARN.  A project counts when the install configures
    ``integration.github_app`` and its repository is on github.com; a local
    remote never uses the App.  A development project is judged on the items
    its publisher needs (credential, repository, protection); the train's
    anchors are listed but not judged until the cutover.  A project the command
    refuses (no binding, no designated repository) is an ERROR: App mode cannot
    run it.  Read-only: the command only reads GitHub through the App.
    """
    from src.integration.train_onboarding import github_full_name

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
        if github_full_name(url) is None:
            continue
        try:
            result = await ctx.handler.execute("integration_app_verify", {"project_id": project.id})
        except Exception as exc:  # noqa: BLE001 - a crashed read is a finding, not a crash
            result = {"success": False, "outcome": "error", "error": f"{type(exc).__name__}: {exc}"}
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
        severity = Severity.ERROR if any(f["status"] == "fail" for f in findings) else Severity.WARN
        return CheckResult(
            id=check_id,
            severity=severity,
            detail="; ".join(
                f"{f['project_id']}: {f['item']} {f['status']} ({', '.join(f['codes'])})"
                for f in findings
            )
            + "; run `aq integration legacy app-verify PROJECT` for each fix",
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


def _age(now: float, then: float | None) -> int | None:
    return None if then is None else max(0, int(now - then))


async def _check_subjects_overdue(ctx: DoctorContext) -> CheckResult:
    """Reconciler-engine subjects due more than one interval ago."""
    from sqlalchemy import func, select

    from src.database.tables import integration_subjects as subjects
    from src.integration.subjects import SubjectEngine

    if ctx.db is None:
        return CheckResult(id=OVERDUE, severity=Severity.INFO,
                           detail="subject schedule unavailable without a database")
    now = time.time()
    live = (subjects.c.engine == SubjectEngine.RECONCILER.value) & (subjects.c.phase != "done")
    late = live & subjects.c.next_due_at.is_not(None) & (
        subjects.c.next_due_at < now - OVERDUE_AFTER_SECONDS
    )
    async with ctx.db._engine.connect() as conn:
        total = (await conn.execute(select(func.count()).where(live))).scalar_one()
        overdue = (await conn.execute(select(func.count()).where(late))).scalar_one()
        rows = (
            await conn.execute(
                select(subjects).where(late).order_by(subjects.c.next_due_at, subjects.c.id)
                .limit(_LISTED)
            )
        ).mappings().all()
    if not total:
        return CheckResult(id=OVERDUE, severity=Severity.INFO,
                           detail="no subject runs on the reconciler engine")
    if not overdue:
        return CheckResult(
            id=OVERDUE, severity=Severity.OK,
            detail=f"{total} reconciler subject(s), none overdue",
            data={"live": total, "overdue": 0},
        )
    listed = [
        {
            "subject_id": row["id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "phase": row["phase"],
            "task_id": row["task_id"],
            "next_due_at": row["next_due_at"],
            "overdue_seconds": _age(now, row["next_due_at"]),
            "last_visit_at": row["last_visit_at"],
            "wait_reason": row["wait_reason"],
        }
        for row in rows
    ]
    oldest = listed[0]
    return CheckResult(
        id=OVERDUE,
        severity=Severity.WARN,
        detail=(
            f"{overdue} of {total} reconciler subject(s) past due by more than "
            f"{int(OVERDUE_AFTER_SECONDS)}s: the reconciler is not visiting "
            f"(oldest {oldest['subject_id']}, {oldest['overdue_seconds']}s); check the daemon "
            "log for subject tick errors and `aq integration explain SUBJECT`"
        ),
        data={"live": total, "overdue": overdue, "subjects": listed},
    )


async def _check_subjects_held(ctx: DoctorContext) -> CheckResult:
    """Open subject gates and operator holds, oldest first."""
    from sqlalchemy import select

    from src.database.tables import gates, task_metadata, tasks
    from src.database.tables import integration_subjects as subjects
    from src.integration.subjects import OPERATOR_HOLD_META_KEY

    if ctx.db is None:
        return CheckResult(id=HELD, severity=Severity.INFO,
                           detail="subject holds unavailable without a database")
    now = time.time()
    async with ctx.db._engine.connect() as conn:
        gate_rows = (
            await conn.execute(
                select(
                    subjects.c.id.label("subject_id"),
                    subjects.c.project_id,
                    subjects.c.kind,
                    subjects.c.task_id,
                    subjects.c.wait_reason,
                    gates.c.id.label("gate_id"),
                    gates.c.title,
                    gates.c.created_at,
                )
                .join(gates, gates.c.id == subjects.c.gate_id)
                .where(gates.c.status == "open", subjects.c.phase != "done")
                .order_by(gates.c.created_at, subjects.c.id)
            )
        ).mappings().all()
        hold_rows = (
            await conn.execute(
                select(task_metadata.c.task_id, task_metadata.c.value, tasks.c.project_id)
                .join(tasks, tasks.c.id == task_metadata.c.task_id)
                .where(task_metadata.c.key == OPERATOR_HOLD_META_KEY)
                .order_by(task_metadata.c.task_id)
            )
        ).mappings().all()
    held = [
        {
            "hold": "gate",
            "subject_id": row["subject_id"],
            "project_id": row["project_id"],
            "kind": row["kind"],
            "task_id": row["task_id"],
            "gate_id": row["gate_id"],
            "title": row["title"],
            "wait_reason": row["wait_reason"],
            "age_seconds": _age(now, row["created_at"]),
            "answer": f"aq integration gate answer {row['gate_id']} CHOICE",
        }
        for row in gate_rows
    ]
    for row in hold_rows:
        try:
            value = json.loads(row["value"])
        except (TypeError, ValueError):
            value = {}
        value = value if isinstance(value, dict) else {}
        held.append(
            {
                "hold": "operator",
                "project_id": row["project_id"],
                "task_id": row["task_id"],
                "reason": value.get("reason"),
                "held_by": value.get("held_by"),
                "age_seconds": _age(now, value.get("held_at")),
                "answer": f"aq integration hold {row['task_id']} --release",
            }
        )
    if not held:
        return CheckResult(id=HELD, severity=Severity.OK,
                           detail="no integration subject is waiting on a person")
    held.sort(key=lambda entry: -(entry["age_seconds"] or 0))
    stale = [entry for entry in held if (entry["age_seconds"] or 0) > STALE_HOLD_SECONDS]
    shown = ", ".join(
        f"{entry.get('gate_id') or entry['task_id']} "
        f"({entry['hold']}, {(entry['age_seconds'] or 0) // 60}m)"
        for entry in held[:5]
    )
    gate_count = sum(1 for entry in held if entry["hold"] == "gate")
    return CheckResult(
        id=HELD,
        # Waiting on a person is intentional; waiting a day is a stalled decision.
        severity=Severity.WARN if stale else Severity.INFO,
        detail=(
            f"{gate_count} open gate(s), {len(held) - gate_count} operator hold(s): {shown}"
            + (f"; {len(stale)} older than a day" if stale else "")
        ),
        data={"held": held[:_LISTED], "gates": gate_count,
              "operator_holds": len(held) - gate_count, "stale": len(stale)},
    )


async def _check_trust(ctx: DoctorContext) -> CheckResult:
    return await app_mode_readiness(ctx, check_id=TRUST)


def integration_subject_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id=OVERDUE, run=_check_subjects_overdue, owner=OWNER),
        DoctorCheck(id=HELD, run=_check_subjects_held, owner=OWNER),
        # app-verify reads GitHub through the App and writes nothing; one run is
        # a handful of API reads per enabled GitHub project.
        DoctorCheck(id=TRUST, run=_check_trust, owner=OWNER, timeout_s=120.0),
    ]
