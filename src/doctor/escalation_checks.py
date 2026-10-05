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

from typing import Any

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
        )
    ]


CHECKS = escalation_checks()
