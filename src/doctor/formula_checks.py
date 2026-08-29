"""``formulas.*`` doctor checks (swarm-work-model §13).

Mirrors ``src/doctor/pool_checks.py``'s shape: a private ``_check_*``
function per check plus a factory that returns the list of
:class:`DoctorCheck`.  Report-only — a malformed formula file is fixed by
editing the vault, not by ``aq doctor --fix``.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "swarm-work-model"


async def _check_formulas_parse(ctx: DoctorContext) -> CheckResult:
    registry = getattr(getattr(ctx.handler, "orchestrator", None), "formula_registry", None)
    if registry is None:
        return CheckResult(
            id="formulas.parse", severity=Severity.INFO, detail="registry not loaded"
        )
    errors = registry.errors
    if not errors:
        return CheckResult(id="formulas.parse", severity=Severity.OK, detail="all formulas parse")
    return CheckResult(
        id="formulas.parse",
        severity=Severity.WARN,
        detail=f"{len(errors)} formula file(s) failed to parse",
        data={"count": len(errors), "files": dict(errors)},
    )


def formula_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(id="formulas.parse", run=_check_formulas_parse, fix=None, owner=OWNER),
    ]
