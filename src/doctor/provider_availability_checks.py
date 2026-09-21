"""``providers.*`` availability checks (``docs/specs/provider-failover.md`` D21).

Four report-only checks over provider availability:

* ``providers.availability`` -- is every provider launchable?  ``WARN`` for a
  degraded or unavailable one (reason, since, expected recovery,
  remediation), ``ERROR`` when every session provider is unavailable, and
  ``INFO`` for a provider an operator ``disabled`` on purpose: an intentional
  state never fails CI.
* ``providers.recovery_stuck`` -- has the recovery loop stopped?  A provider
  past its ``until`` + ``reset_grace_seconds`` by more than two orchestrator
  sweeps without entering probation, or ``unauthenticated`` with no auth
  probe for three probe intervals.
* ``providers.failover_playbook`` -- re-routing is configured on but the
  ``provider-failover`` playbook that moves held work is not active.
* ``providers.held_tasks`` -- queued work held by an unavailable provider for
  longer than ``provider_failover.doctor.held_warn_seconds``.

Report-only, by design: every useful fix here either disrupts live sessions
or papers over a stalled loop -- the reasoning ``providers.claude_usage``
already records for having none.  Each check reads the daemon's live
snapshot when a handler is wired, and otherwise loads the stored rows itself,
so ``aq doctor`` works with the daemon down.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

from src.doctor.models import CheckResult, DoctorCheck, DoctorContext, Severity

logger = logging.getLogger(__name__)

OWNER = "provider-failover"

AVAILABILITY_CHECK_ID = "providers.availability"
RECOVERY_STUCK_CHECK_ID = "providers.recovery_stuck"
FAILOVER_PLAYBOOK_CHECK_ID = "providers.failover_playbook"
HELD_TASKS_CHECK_ID = "providers.held_tasks"

#: The playbook that moves held work (``bold-rapids.3``).
FAILOVER_PLAYBOOK_ID = "provider-failover"
#: One orchestrator cycle, seconds; ``recovery_stuck`` allows two of them.
ORCHESTRATOR_SWEEP_SECONDS = 5.0
#: Provider keys that are not session harnesses (D13a's direct path).
_NON_SESSION_PROVIDERS = frozenset({"llm"})


def _fmt(ts: float | None) -> str:
    if not ts:
        return "unknown"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _age(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 86400:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


async def _service(ctx: DoctorContext) -> Any:
    """The daemon's availability service, or a read-only one loaded from the db."""
    orchestrator = getattr(ctx.handler, "orchestrator", None)
    live = getattr(orchestrator, "provider_availability", None)
    if live is not None:
        return live
    if ctx.db is None or not hasattr(ctx.db, "list_provider_availability"):
        return None
    from src.providers.availability_service import ProviderAvailabilityService

    service = ProviderAvailabilityService(db=ctx.db, config_getter=lambda: ctx.config)
    await service.load()
    return service


def _off(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="provider_failover.mode is off; provider availability is not tracked",
    )


def _unavailable_result(check_id: str) -> CheckResult:
    return CheckResult(
        id=check_id,
        severity=Severity.INFO,
        detail="provider availability tables are not available",
    )


# -- providers.availability ----------------------------------------------------


async def _check_availability(ctx: DoctorContext) -> CheckResult:
    service = await _service(ctx)
    if service is None:
        return _unavailable_result(AVAILABILITY_CHECK_ID)
    if not service.tracking:
        return _off(AVAILABILITY_CHECK_ID)
    from src.providers.availability import AVAILABLE, DISABLED, UNAVAILABLE

    now = service.now()
    rows = service.rows()
    if not rows:
        return CheckResult(
            id=AVAILABILITY_CHECK_ID,
            severity=Severity.OK,
            detail="no provider has recorded availability yet",
        )
    views = {provider: service.describe(provider, now) for provider in sorted(rows)}
    problems = {p: v for p, v in views.items() if v["state"] != AVAILABLE}
    data = {"providers": views, "mode": getattr(service.config, "mode", "enforce")}
    if not problems:
        return CheckResult(
            id=AVAILABILITY_CHECK_ID,
            severity=Severity.OK,
            detail="every provider is available: " + ", ".join(sorted(views)),
            data=data,
        )

    def line(provider: str, view: dict) -> str:
        text = f"{provider} {view['state']} since {_fmt(view['since'])}"
        if view.get("reason"):
            text += f" ({view['reason']})"
        if view.get("until"):
            text += f", expected recovery {_fmt(view['until'])}"
        if view.get("remediation"):
            text += f"; {view['remediation']}"
        return text

    detail = "; ".join(line(p, v) for p, v in problems.items())
    session = {p: v for p, v in views.items() if p not in _NON_SESSION_PROVIDERS}
    intentional = {
        p for p, v in problems.items() if v["state"] == DISABLED and v.get("override")
    }
    all_down = bool(session) and all(v["state"] in UNAVAILABLE for v in session.values())
    if all_down and not set(session) <= intentional:
        return CheckResult(
            id=AVAILABILITY_CHECK_ID,
            severity=Severity.ERROR,
            detail="every session provider is unavailable -- nothing can launch: " + detail,
            data=data,
        )
    if set(problems) <= intentional:
        return CheckResult(
            id=AVAILABILITY_CHECK_ID,
            severity=Severity.INFO,
            detail="disabled by an operator override: " + detail,
            data=data,
        )
    return CheckResult(
        id=AVAILABILITY_CHECK_ID, severity=Severity.WARN, detail=detail, data=data
    )


# -- providers.recovery_stuck --------------------------------------------------


async def _check_recovery_stuck(ctx: DoctorContext) -> CheckResult:
    service = await _service(ctx)
    if service is None:
        return _unavailable_result(RECOVERY_STUCK_CHECK_ID)
    if not service.tracking:
        return _off(RECOVERY_STUCK_CHECK_ID)
    from src.providers.availability import EXHAUSTED, FAILING, UNAUTHENTICATED

    cfg = service.config
    now = service.now()
    grace = float(cfg.recovery.reset_grace_seconds)
    probe_every = float(cfg.recovery.auth_probe_interval_seconds)
    stuck: list[dict[str, Any]] = []
    for provider, row in sorted(service.rows().items()):
        if row.state in (EXHAUSTED, FAILING) and row.until is not None:
            late = now - (row.until + grace)
            if late > 2 * ORCHESTRATOR_SWEEP_SECONDS:
                stuck.append(
                    {
                        "provider": provider,
                        "state": row.state,
                        "problem": "past_recovery",
                        "detail": (
                            f"{provider} is still {row.state} {_age(late)} after its expected "
                            f"recovery ({_fmt(row.until)}) without entering probation"
                        ),
                    }
                )
        elif row.state == UNAUTHENTICATED and provider not in _NON_SESSION_PROVIDERS:
            last = max(float(row.last_probe_at or 0.0), float(row.since or 0.0))
            if now - last > 3 * probe_every:
                stuck.append(
                    {
                        "provider": provider,
                        "state": row.state,
                        "problem": "no_auth_probe",
                        "detail": (
                            f"{provider} is unauthenticated and no login probe has answered "
                            f"for {_age(now - last)} (expected every {_age(probe_every)})"
                        ),
                    }
                )
    if not stuck:
        return CheckResult(
            id=RECOVERY_STUCK_CHECK_ID,
            severity=Severity.OK,
            detail="every unavailable provider's recovery is on schedule",
        )
    return CheckResult(
        id=RECOVERY_STUCK_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            "the provider recovery loop has stopped: "
            + "; ".join(item["detail"] for item in stuck)
            + ". Check the daemon is running its cycle; `aq provider recheck <provider>` "
            "probes now."
        ),
        data={"stuck": stuck},
    )


# -- providers.failover_playbook -----------------------------------------------


def _shipped_failover_bundle() -> bool:
    """Does this daemon carry the ``provider-failover`` playbook at all?"""
    root = Path(__file__).resolve().parents[1] / "prompts"
    return (root / "reviewed_playbooks" / FAILOVER_PLAYBOOK_ID).exists() or (
        root / "default_playbooks" / f"{FAILOVER_PLAYBOOK_ID}.md"
    ).exists()


async def _check_failover_playbook(ctx: DoctorContext) -> CheckResult:
    from src.config import ProviderFailoverConfig

    cfg = getattr(ctx.config, "provider_failover", None) or ProviderFailoverConfig()
    if not (cfg.mode == "enforce" and cfg.reroute.enabled):
        return CheckResult(
            id=FAILOVER_PLAYBOOK_CHECK_ID,
            severity=Severity.INFO,
            detail=(
                f"re-routing is off (provider_failover.mode={cfg.mode}, "
                f"reroute.enabled={cfg.reroute.enabled}); held tasks wait for their provider"
            ),
        )
    activations: list[dict] = []
    db = ctx.db
    if db is not None and hasattr(db, "list_playbook_activations"):
        try:
            activations = [
                row
                for row in await db.list_playbook_activations()
                if row.get("playbook_id") == FAILOVER_PLAYBOOK_ID
            ]
        except Exception:  # an unreadable table reads as "not activated"
            logger.debug("failover_playbook: activations unreadable", exc_info=True)
    active = [
        row for row in activations if row.get("enabled") and row.get("active_artifact_sha256")
    ]
    if active:
        return CheckResult(
            id=FAILOVER_PLAYBOOK_CHECK_ID,
            severity=Severity.OK,
            detail=f"the {FAILOVER_PLAYBOOK_ID} playbook is active",
        )
    if not activations and not _shipped_failover_bundle():
        return CheckResult(
            id=FAILOVER_PLAYBOOK_CHECK_ID,
            severity=Severity.INFO,
            detail=(
                "the re-route engine is not shipped on this install; tasks held by an "
                "unavailable provider hold and do not move"
            ),
        )
    return CheckResult(
        id=FAILOVER_PLAYBOOK_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"provider_failover.mode is enforce with reroute.enabled, but the "
            f"{FAILOVER_PLAYBOOK_ID} playbook is not active: tasks will hold and never move. "
            f"Activate it (`aq playbook activate --playbook-id {FAILOVER_PLAYBOOK_ID} ...`) "
            "or set reroute.enabled: false."
        ),
        data={"activations": [row.get("activation_id") for row in activations]},
    )


# -- providers.held_tasks ------------------------------------------------------


async def _check_held_tasks(ctx: DoctorContext) -> CheckResult:
    service = await _service(ctx)
    if service is None:
        return _unavailable_result(HELD_TASKS_CHECK_ID)
    if not service.tracking:
        return _off(HELD_TASKS_CHECK_ID)
    suppressed = service.suppressed_providers()
    if not suppressed:
        return CheckResult(
            id=HELD_TASKS_CHECK_ID,
            severity=Severity.OK,
            detail="no provider is holding work",
        )
    from src.models import TaskStatus

    threshold = float(service.config.doctor.held_warn_seconds)
    now = service.now()
    held: list[dict[str, Any]] = []
    for status in (TaskStatus.READY, TaskStatus.DEFINED, TaskStatus.BLOCKED, TaskStatus.PAUSED):
        try:
            tasks = await ctx.db.list_tasks(status=status)
        except Exception:  # a status we cannot list holds nothing we can report
            logger.debug("held_tasks: %s tasks unreadable", status, exc_info=True)
            continue
        for task in tasks:
            hold = await service.hold_for(task)
            if hold is None:
                continue
            since = float(hold.get("since") or now)
            held.append(
                {
                    "task_id": task.id,
                    "project_id": task.project_id,
                    "provider": hold["provider"],
                    "kind": hold["kind"],
                    "held_seconds": max(0.0, now - since),
                }
            )
    if not held:
        return CheckResult(
            id=HELD_TASKS_CHECK_ID,
            severity=Severity.OK,
            detail="unavailable provider(s) "
            + ", ".join(sorted(suppressed))
            + " hold no queued task",
        )
    long_held = [item for item in held if item["held_seconds"] > threshold]
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for item in long_held or held:
        by_kind.setdefault(item["kind"], []).append(item)
    summary = "; ".join(
        f"{kind}: {len(items)} task(s), longest {_age(max(i['held_seconds'] for i in items))}"
        for kind, items in sorted(by_kind.items())
    )
    data = {"held": held, "by_kind": {k: [i["task_id"] for i in v] for k, v in by_kind.items()}}
    if not long_held:
        return CheckResult(
            id=HELD_TASKS_CHECK_ID,
            severity=Severity.OK,
            detail=f"{len(held)} task(s) held, none longer than {_age(threshold)} ({summary})",
            data=data,
        )
    return CheckResult(
        id=HELD_TASKS_CHECK_ID,
        severity=Severity.WARN,
        detail=(
            f"{len(long_held)} task(s) held by an unavailable provider for longer than "
            f"{_age(threshold)}: {summary}. `aq task explain <id>` names the hold."
        ),
        data=data,
    )


def provider_availability_checks() -> list[DoctorCheck]:
    # Report-only: no ``fix`` (see the module docstring).
    return [
        DoctorCheck(id=AVAILABILITY_CHECK_ID, run=_check_availability, owner=OWNER),
        DoctorCheck(id=RECOVERY_STUCK_CHECK_ID, run=_check_recovery_stuck, owner=OWNER),
        DoctorCheck(id=FAILOVER_PLAYBOOK_CHECK_ID, run=_check_failover_playbook, owner=OWNER),
        DoctorCheck(id=HELD_TASKS_CHECK_ID, run=_check_held_tasks, owner=OWNER),
    ]


__all__ = [
    "AVAILABILITY_CHECK_ID",
    "FAILOVER_PLAYBOOK_CHECK_ID",
    "HELD_TASKS_CHECK_ID",
    "OWNER",
    "RECOVERY_STUCK_CHECK_ID",
    "provider_availability_checks",
]
