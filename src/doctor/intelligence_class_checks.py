"""``intelligence_classes.*`` doctor checks.

Report-only, mirroring :mod:`src.doctor.formula_checks`: a malformed
``vault/intelligence-classes/<id>.md`` is fixed by editing the vault, not by
``aq doctor --fix``.  The registry keeps the previous entry for a file that
stops parsing, so the warning is the only signal that the file on disk and
the class the daemon launches with have diverged.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "intelligence-classes"


async def _check_intelligence_classes_parse(ctx: DoctorContext) -> CheckResult:
    registry = getattr(
        getattr(ctx.handler, "orchestrator", None), "intelligence_classes", None
    )
    if registry is None:
        return CheckResult(
            id="intelligence_classes.parse",
            severity=Severity.INFO,
            detail="registry not loaded",
        )
    errors = getattr(registry, "errors", {})
    if not errors:
        return CheckResult(
            id="intelligence_classes.parse",
            severity=Severity.OK,
            detail=f"{len(registry)} intelligence class(es) loaded",
            data={"count": len(registry)},
        )
    return CheckResult(
        id="intelligence_classes.parse",
        severity=Severity.WARN,
        detail=f"{len(errors)} intelligence-class file(s) failed to parse",
        data={"count": len(errors), "files": dict(errors)},
    )


def intelligence_class_checks() -> list[DoctorCheck]:
    return [
        DoctorCheck(
            id="intelligence_classes.parse",
            run=_check_intelligence_classes_parse,
            fix=None,
            owner=OWNER,
        ),
    ]
