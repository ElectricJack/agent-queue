"""``escalations.*`` doctor checks: is the human channel still a log?

The one channel is shared, so the pile §5.6 describes -- every incident the
daemon ever raised, none of them ever edited -- is the operator's problem to
see rather than something they have to go and count.  ``escalations.pile``
answers it in one line:

* **WARN** when the §5.6 sweep still has work: incidents it would close, and
  old questions it would list for supervisor triage.  ``--fix`` runs exactly the
  plan a dry run prints, so the fix is never a surprise.
* **INFO** when there is nothing left to sweep but the channel still carries
  more open items than §5.6 step 5's target.  That residue is live gates and
  questions a supervisor has not triaged; neither is a fault the fix could
  remove, so it is reported, not fixed.
* **OK** when the pile is inside the target and the sweep has nothing to do.
* **INFO** when ``discord.escalations.stateful`` is off (§7.1's rollback), since
  then the create-only behaviour is what the operator asked for.

The check reads the same :class:`~src.escalations.sweep.EscalationSweeper` the
``aq escalation sweep`` command runs, which is what makes "the dry run matches
the apply" checkable rather than aspirational.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from sqlalchemy import exists, select

from src.dashboard_paths import escalation_path
from src.database.tables import escalation_deliveries, escalations
from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.escalations.sweep import SCAN_LIMIT, TARGET_OPEN_ITEMS, EscalationSweeper

CHECK_ID = "escalations.pile"
OWNER = "discord-escalations"

#: How many planned rows a result carries.  The counts are exact; the list is a
#: sample, so ``--json`` stays readable on a pile the size §1.5 counted.
_LIST = 20

#: The pile check reads one row per incident plus its sources, so it is given
#: longer than doctor's 5 s default: 81 rows is a few hundred small queries.
_TIMEOUT_S = 30.0
UNDELIVERED_CHECK_ID = "escalations.undelivered"
UNDELIVERED_AFTER_SECONDS = 5 * 60


async def undelivered_escalations(
    ctx: DoctorContext, *, now: float | None = None, project_ids: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Read the entire overdue set; a newest-incident scan can hide old gates."""
    if ctx.db is None:
        return []
    now = time.time() if now is None else now
    confirmed_root = exists().where(
        escalation_deliveries.c.escalation_id == escalations.c.id,
        escalation_deliveries.c.kind == "root",
        escalation_deliveries.c.status == "sent",
        escalation_deliveries.c.receipt_confirmed_at.is_not(None),
    )
    statement = select(escalations).where(
        escalations.c.state.in_(("needs_human", "reply_received", "resolving")),
        escalations.c.source_kind != "supervisor_delivery",
        escalations.c.created_at < now - UNDELIVERED_AFTER_SECONDS,
        ~confirmed_root,
    ).order_by(escalations.c.created_at, escalations.c.id)
    if project_ids is not None:
        statement = statement.where(escalations.c.project_id.in_(tuple(project_ids)))
    async with ctx.db._engine.connect() as conn:
        incidents = (await conn.execute(statement)).mappings().all()
        ids = [row["id"] for row in incidents]
        roots = []
        if ids:
            roots = (await conn.execute(select(escalation_deliveries).where(
                escalation_deliveries.c.escalation_id.in_(ids),
                escalation_deliveries.c.kind == "root",
            ).order_by(
                escalation_deliveries.c.generation, escalation_deliveries.c.updated_at,
                escalation_deliveries.c.id,
            ))).mappings().all()
    latest = {row["escalation_id"]: row for row in roots}
    findings = []
    for incident in incidents:
        root = latest.get(incident["id"])
        status = root["status"] if root else "missing"
        age = now - incident["created_at"]
        findings.append({
            "project_id": incident["project_id"], "escalation_id": incident["id"],
            "task_id": incident["task_id"], "title": incident["task_title"],
            "deep_link": escalation_path(incident["id"]), "age_seconds": age,
            "delivery_status": status, "last_error": root["last_error"] if root else None,
            "detail": f"{incident['id']} has no confirmed human delivery after {age / 60:.1f} "
                      f"minutes (root: {status})",
        })
    return findings


async def _check_undelivered(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(UNDELIVERED_CHECK_ID, Severity.INFO, "database not configured")
    findings = await undelivered_escalations(ctx)
    return CheckResult(
        UNDELIVERED_CHECK_ID, Severity.WARN if findings else Severity.OK,
        f"{len(findings)} human escalation(s) undelivered for more than five minutes",
        data={"findings": findings, "count": len(findings)},
    )


def _sweeper(ctx: DoctorContext) -> EscalationSweeper | None:
    if ctx.db is None:
        return None
    return EscalationSweeper(ctx.db, ctx.config)


def _plan_result(plan: Any, *, fixable: bool) -> CheckResult:
    """Grade one dry run.  Pure: it renders, it does not write."""
    open_before = plan.open_before
    closable = len(plan.closable)
    triage = len(plan.triage)
    data = {
        "open": open_before,
        "target_open_items": TARGET_OPEN_ITEMS,
        "inspected": plan.inspected,
        "counts": plan.counts,
        "closable": closable,
        "triage": triage,
        "planned": [item.to_dict() for item in plan.items[:_LIST]],
        "untouched": [dict(row) for row in plan.untouched[:_LIST]],
        "untouched_count": len(plan.untouched),
    }
    if plan.items:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=(
                f"{open_before} open escalation(s); the §5.6 sweep would close {closable} and "
                f"list {triage} for supervisor triage — run `aq escalation sweep` to review, "
                "then `--apply`"
            ),
            fixable=fixable,
            data=data,
        )
    if open_before > TARGET_OPEN_ITEMS:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail=(
                f"{open_before} open escalation(s) and nothing left for the §5.6 sweep: the "
                f"residue is live gates and untriaged questions (target is "
                f"{TARGET_OPEN_ITEMS})"
            ),
            data=data,
        )
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        detail=f"{open_before} open escalation(s), within the §5.6 target of {TARGET_OPEN_ITEMS}",
        data=data,
    )


async def _check_pile(ctx: DoctorContext) -> CheckResult:
    sweeper = _sweeper(ctx)
    if sweeper is None:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="database not configured; the escalation pile is not checked",
        )
    if not sweeper.enabled:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="discord.escalations.stateful is off; the pile is left as it is",
        )
    return _plan_result(await sweeper.plan(limit=SCAN_LIMIT), fixable=True)


async def _fix_pile(ctx: DoctorContext) -> CheckResult:
    """Run the plan this check just printed, then re-read the pile."""
    sweeper = _sweeper(ctx)
    if sweeper is None or not sweeper.enabled:
        return await _check_pile(ctx)
    plan = await sweeper.plan(limit=SCAN_LIMIT)
    report = await sweeper.apply(plan)
    result = _plan_result(await sweeper.plan(limit=SCAN_LIMIT), fixable=True)
    result.fix_applied = report.closed > 0 or report.triaged > 0
    result.data["swept"] = report.to_dict()
    if report.failures:
        result.detail += f"; {len(report.failures)} row(s) failed — see swept.failures"
    return result


def escalation_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id=CHECK_ID,
            run=_check_pile,
            fix=_fix_pile,
            timeout_s=_TIMEOUT_S,
            owner=OWNER,
        ),
        DoctorCheck(id=UNDELIVERED_CHECK_ID, run=_check_undelivered, owner=OWNER),
    ]


CHECKS = escalation_checks()
