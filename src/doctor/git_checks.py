"""git.* doctor checks — remote branches nothing needs any more.

``git.stale_branches`` is the branch policy's backlog view, and what the
supervisor's stall sweep runs.  New development deliveries clean up after
themselves (``DevelopmentIntegration.collect_delivered_branches``); this check
finds everything else, for each development project's origin:

* ``aq/`` branches whose work is on the default branch;
* ``aq/integration/*`` refs whose owner row is ``released`` and whose
  operation finished;
* branches of FAILED or abandoned tasks, 14 days after they went terminal —

minus everything ``live_branch_references`` holds.  ``--fix`` deletes them,
backing every tip the default branch cannot reach up to a bundle and logging
every branch with its sha first.  It never touches anything outside ``aq/``,
the default branch, ``main`` or ``gh-pages``.  The rules live in
``src/integration/delivery_branches.py``; this module only reports.

Mirrors ``src/doctor/integration_checks.py``'s shape: ``_check_*``/``_fix_*``,
a factory returning the :class:`DoctorCheck` list, a ``CHECKS`` snapshot and
a ``run_check`` wrapper for tests and ad-hoc calls.
"""

from __future__ import annotations

from collections import Counter

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

OWNER = "git"
CHECK_ID = "git.stale_branches"

#: A scan fetches each development project's origin and runs one ``git log``
#: per branch not merged by ancestry; its fix bundles and pushes the deletes.
_TIMEOUT_S = 300.0
#: Branch names listed per project in ``data``; counts stay exact.
_EXAMPLES = 20


def _development_publisher(ctx: DoctorContext):
    """The daemon's publisher when doctor runs inside it, else a fresh one."""
    factory = getattr(ctx.handler, "_development_integration", None)
    if factory is not None:
        return factory()
    from src.integration.development import DevelopmentIntegration

    return DevelopmentIntegration(ctx.db, data_dir=ctx.config.data_dir)


async def _scan(ctx: DoctorContext, *, delete: bool = False):
    """One :meth:`DevelopmentIntegration.stale_branches` report per project.

    Development projects only: their publisher keeps the clone this needs,
    and they are the projects whose delivery leaves branches behind.  A
    project whose remote cannot be read is reported, never allowed to hide
    the others.
    """
    from sqlalchemy import select

    from src.database.tables import projects

    async with ctx.db._engine.connect() as conn:
        project_ids = list(
            (
                await conn.execute(
                    select(projects.c.id)
                    .where(
                        projects.c.hierarchical_integration_mode == "development",
                        projects.c.integration_repository_id.is_not(None),
                    )
                    .order_by(projects.c.id)
                )
            ).scalars()
        )
    publisher = _development_publisher(ctx) if project_ids else None
    reports, errors = [], []
    for project_id in project_ids:
        try:
            reports.append(await publisher.stale_branches(project_id, delete=delete))
        except Exception as exc:  # noqa: BLE001 - one remote must not hide the rest
            errors.append({"project_id": project_id, "error": f"{type(exc).__name__}: {exc}"})
    return reports, errors


def _summary(report: dict) -> dict:
    stale = report["stale"]
    summary = {
        "project_id": report["project_id"],
        "repository_id": report["repository_id"],
        "stale": len(stale),
        "by_rule": dict(Counter(e["rule"] for e in stale)),
        "by_subject": sum(1 for e in stale if e["found_by"] == "subject"),
        "held": len(report["held"]),
        "kept": report["kept"],
        "out_of_scope": report["out_of_scope"],
        "branches": [
            {"branch": e["branch"], "rule": e["rule"], "reason": e["reason"]}
            for e in stale[:_EXAMPLES]
        ],
        "held_examples": [
            {"branch": e["branch"], "rule": e["rule"], "held_by": e["held_by"]}
            for e in report["held"][:_EXAMPLES]
        ],
    }
    if "deleted" in report:
        summary.update(
            deleted=len(report["deleted"]),
            moved=report["moved"],
            failed=report["failed"],
            backup=report["backup"],
        )
    return summary


async def _check_stale_branches(ctx: DoctorContext) -> CheckResult:
    if ctx.db is None:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail="database not initialised — origin branches not checked",
        )
    reports, errors = await _scan(ctx)
    count = sum(len(r["stale"]) for r in reports)
    data = {
        "count": count,
        "projects": [_summary(r) for r in reports],
        "errors": errors,
    }
    if count:
        first = next(r["stale"][0] for r in reports if r["stale"])
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.WARN,
            detail=(
                f"{count} aq/ branch(es) on origin are stale and nothing references them "
                f"— e.g. {first['branch']} ({first['reason']}). "
                f"`aq doctor --check {CHECK_ID} --fix` backs them up and deletes them"
            ),
            fixable=True,
            data=data,
        )
    if errors:
        return CheckResult(
            id=CHECK_ID,
            severity=Severity.INFO,
            detail=(
                f"could not read origin for {len(errors)} project(s) — e.g. "
                f"{errors[0]['project_id']}: {errors[0]['error']}"
            ),
            data=data,
        )
    held = sum(len(r["held"]) for r in reports)
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.OK,
        detail=(
            "no stale aq/ branch is left on origin"
            + (f" ({held} would be, but are still referenced)" if held else "")
        ),
        data=data,
    )


async def _fix_stale_branches(ctx: DoctorContext) -> CheckResult:
    """Delete every stale, unreferenced branch the scan finds right now.

    The fix re-scans rather than trusting the check's snapshot: holds and
    remote heads are read again under the publisher's exclusion.  Each
    delete is backed up first when the default branch cannot reach it,
    logged with its sha, and made on a lease at the head the scan saw; a
    branch that moved in between is left alone and reported.
    """
    reports, errors = await _scan(ctx, delete=True)
    deleted = sum(len(r.get("deleted", [])) for r in reports)
    failed = [b for r in reports for b in r.get("failed", [])]
    moved = [b for r in reports for b in r.get("moved", [])]
    bundles = [r["backup"]["bundle"] for r in reports if r.get("backup", {}).get("bundle")]
    logs = sorted({r["backup"]["log"] for r in reports if r.get("backup", {}).get("log")})
    detail = f"deleted {deleted} stale branch(es) from origin"
    if bundles:
        detail += f"; unmerged tips backed up to {', '.join(bundles)}"
    if logs:
        detail += f"; logged in {', '.join(logs)}"
    if moved:
        detail += f"; {len(moved)} moved while being deleted and were kept"
    if failed:
        detail += f"; {len(failed)} delete(s) not confirmed (e.g. {failed[0]}) — run again"
    if errors:
        detail += f"; {len(errors)} project(s) not reached"
    return CheckResult(
        id=CHECK_ID,
        severity=Severity.WARN if failed or errors else Severity.OK,
        detail=detail,
        fixable=True,
        fix_applied=True,
        data={
            "deleted": deleted,
            "projects": [_summary(r) for r in reports],
            "errors": errors,
        },
    )


def git_checks() -> list[DoctorCheck]:
    return [
        # Fixable, and the fix deletes remote branches — but only ``aq/``
        # ones the branch policy lets go of and that nothing in
        # ``live_branch_references`` holds, each backed up (when unmerged),
        # logged, and deleted on a lease at its observed head.
        DoctorCheck(
            id=CHECK_ID,
            run=_check_stale_branches,
            fix=_fix_stale_branches,
            owner=OWNER,
            timeout_s=_TIMEOUT_S,
        ),
    ]


#: Snapshot for call-sites (tests, ad-hoc scripts) that want the list without
#: building a full :class:`~src.doctor.runner.DoctorRegistry`.
CHECKS = git_checks()

_BY_ID = {c.id: c for c in CHECKS}


async def run_check(db, check_id: str, *, config=None, handler=None) -> CheckResult:
    """Run one git check directly against *db* (no registry needed)."""
    check = _BY_ID[check_id]
    return await check.run(DoctorContext(config=config, db=db, handler=handler))
