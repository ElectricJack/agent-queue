"""``resources.*`` doctor checks — is this box actually being saturated?

Same shape as ``src/doctor/pool_checks.py``: a private ``_check_*`` per
check, a factory returning the :class:`DoctorCheck` list, a ``CHECKS``
snapshot and a ``run_check`` wrapper for tests.

The checks exist because the symptom (agents dying) shows up a long way
from the cause (one session running ``pytest -n auto``), and the operator's
first question is always "which session?".  Every finding here names the
sessions responsible, resolved from ``/proc`` — worktree slot, task id,
session name — rather than reporting an anonymous number.
"""

from __future__ import annotations

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity
from src.doctor.runner import apply_fix
from src.resources.limits import cgroup_delegation
from src.resources.procs import load_average, pytest_processes, summarize_by_session

OWNER = "resource-gating"


def _resources(ctx: DoctorContext):
    return getattr(ctx.config, "resources", None)


# ---------------------------------------------------------------------------
# resources.load
# ---------------------------------------------------------------------------


async def _check_resources_load(ctx: DoctorContext) -> CheckResult:
    """Warn when the box has been oversubscribed for the last five minutes.

    The *5-minute* average is the one that matters: a 1-minute spike is a
    build starting, while five minutes above one-runnable-task-per-core is
    a box where every agent is now slower than it needs to be and the OOM
    killer is the next event.
    """
    res = _resources(ctx)
    if res is None or not getattr(res, "enabled", False):
        return CheckResult(
            id="resources.load",
            severity=Severity.INFO,
            detail="resource gating is disabled (resources.enabled = false)",
        )
    load = load_average()
    if load is None:  # pragma: no cover - non-POSIX
        return CheckResult(
            id="resources.load",
            severity=Severity.INFO,
            detail="load average is not available on this platform",
        )
    one, five, fifteen = load
    cores = res.core_count()
    threshold = cores * float(res.load_warn_ratio)
    data = {
        "load1": round(one, 2),
        "load5": round(five, 2),
        "load15": round(fifteen, 2),
        "cores": cores,
        "threshold": round(threshold, 2),
    }
    if five <= threshold:
        return CheckResult(
            id="resources.load",
            severity=Severity.OK,
            detail=f"5-min load {five:.2f} of {cores} core(s)",
            data=data,
        )
    busiest = summarize_by_session(pytest_processes())
    data["sessions"] = busiest
    who = ", ".join(f"{row['owner']} ({row['count']})" for row in busiest[:4])
    return CheckResult(
        id="resources.load",
        severity=Severity.WARN,
        detail=(
            f"5-min load {five:.2f} exceeds {threshold:.2f} on {cores} core(s)"
            + (f" — pytest processes: {who}" if who else "")
        ),
        data=data,
    )


# ---------------------------------------------------------------------------
# resources.test_pressure
# ---------------------------------------------------------------------------


async def _check_resources_test_pressure(ctx: DoctorContext) -> CheckResult:
    """Warn when more pytest processes are running than the box can absorb.

    This is the direct reading of the failure mode the semaphore exists to
    prevent, so it stays useful even when load has not caught up yet — a
    fan-out of 192 xdist workers is visible in the process table seconds
    before it is visible in the load average.
    """
    res = _resources(ctx)
    if res is None or not getattr(res, "enabled", False):
        return CheckResult(
            id="resources.test_pressure",
            severity=Severity.INFO,
            detail="resource gating is disabled (resources.enabled = false)",
        )
    procs = pytest_processes()
    limit = int(res.max_pytest_processes)
    sessions = summarize_by_session(procs)
    data = {"count": len(procs), "limit": limit, "sessions": sessions}
    if limit <= 0 or len(procs) <= limit:
        return CheckResult(
            id="resources.test_pressure",
            severity=Severity.OK,
            detail=f"{len(procs)} pytest process(es), limit {limit}",
            data=data,
        )
    who = ", ".join(f"{row['owner']} ({row['count']})" for row in sessions[:4])
    return CheckResult(
        id="resources.test_pressure",
        severity=Severity.WARN,
        detail=(
            f"{len(procs)} pytest process(es) running, above the {limit} expected "
            f"for this box — {who or 'no session could be identified'}. "
            "Agents should run heavy suites through `aq test`."
        ),
        data=data,
    )


# ---------------------------------------------------------------------------
# resources.cgroups
# ---------------------------------------------------------------------------


async def _check_resources_cgroups(ctx: DoctorContext) -> CheckResult:
    """Report whether hard per-session limits are actually in force.

    Not having them is a normal, supported state (layer 3 needs a one-time
    root step), so absence is ``info`` — but asking for them and silently
    not getting them is a ``warn``, because that is the configuration an
    operator believes is protecting them when it is not.
    """
    res = _resources(ctx)
    cg = getattr(res, "cgroups", None) if res is not None else None
    delegation = cgroup_delegation()
    data = {
        "enabled": bool(getattr(cg, "enabled", False)),
        "delegated": delegation.available,
        "reason": delegation.reason,
    }
    if cg is None or not cg.enabled:
        return CheckResult(
            id="resources.cgroups",
            severity=Severity.INFO,
            detail=(
                "hard per-session limits are off (resources.cgroups.enabled = false); "
                "env caps and nice still apply"
            ),
            data=data,
        )
    if not delegation.available:
        return CheckResult(
            id="resources.cgroups",
            severity=Severity.WARN,
            detail=(
                f"resources.cgroups.enabled is set but scopes cannot be created "
                f"({delegation.reason}) — sessions are running with env caps only. "
                "Run scripts/setup-cgroup-delegation.sh once as root."
            ),
            data=data,
        )
    data["cpu_quota_percent"] = cg.cpu_quota_percent
    data["memory_max"] = cg.memory_max
    return CheckResult(
        id="resources.cgroups",
        severity=Severity.OK,
        detail=(
            f"sessions launch in a scope with CPUQuota={cg.cpu_quota_percent}% "
            f"MemoryMax={cg.memory_max}"
        ),
        data=data,
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resource_checks() -> list[DoctorCheck]:
    """Every ``resources.*`` check."""
    return [
        DoctorCheck(id="resources.load", run=_check_resources_load, owner=OWNER, timeout_s=10.0),
        DoctorCheck(
            id="resources.test_pressure",
            run=_check_resources_test_pressure,
            owner=OWNER,
            timeout_s=10.0,
        ),
        DoctorCheck(
            id="resources.cgroups", run=_check_resources_cgroups, owner=OWNER, timeout_s=15.0
        ),
    ]


CHECKS = {check.id: check for check in resource_checks()}


async def run_check(check_id: str, ctx: DoctorContext, *, repair: bool = False) -> CheckResult:
    """Run one ``resources.*`` check by id without building a registry."""
    check = CHECKS[check_id]
    if repair and check.fix is not None:
        return await apply_fix(check, ctx)
    return await check.run(ctx)
