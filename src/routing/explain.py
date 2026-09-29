"""Why the router has not routed a task: ``aq task explain`` (mandatory routing §10).

A task the router still owes a route -- ``unrouted``, or ``legacy`` in a
project whose router is ready -- explains as exactly one of:

* ``router_unbound`` -- the project names no routing playbook;
* ``router_not_ready`` -- its router has no enabled activation granting
  ``task_route_apply`` (§9.1), so nothing routes and the task waits;
* ``route_failed`` -- the router's latest run for the task failed;
* ``route_no_candidates`` -- that run found no worker candidate, with the
  planner's reason (``workspace_requirement``,
  ``preferred_provider_unavailable``, a policy rule);
* ``route_held`` -- that run held the task because every candidate's
  provider cannot launch now, naming the providers;
* ``awaiting_route`` -- the router has not answered yet, with the time since
  ``task.route_needed`` was last emitted.

The router's answer is read from its run: runs have no task column, so the
caller finds the newest ``task.route_needed`` run whose event names the task
(``latest_router_run_for_task``), and this module reads the ``plan_a`` /
``plan_b`` / ``plan_c`` bindings its ``task_route_plan`` steps saved.  A
binding holds the plan's value, not its outcome, so the outcome is read from
the value's shape.  Pure: no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

#: The routing playbook's ``task_route_plan`` bindings, latest step first:
#: ``plan_b`` (after classification) and ``plan_c`` (after a failed one) both
#: follow ``plan_a``, and a run takes at most one of them.
PLAN_BINDINGS: tuple[str, ...] = ("plan_c", "plan_b", "plan_a")
#: Run lifecycles that ended without the rule completing.
FAILED_LIFECYCLES = frozenset({"failed", "timed_out", "cancelled"})
#: Run lifecycles still in flight.
LIVE_LIFECYCLES = frozenset({"running", "paused", "cancelling", "blocked"})


def plan_outcome(value: Any) -> str | None:
    """The ``task_route_plan`` outcome a saved plan *value* came from, or ``None``."""
    if not isinstance(value, Mapping):
        return None
    if value.get("profile_id"):
        return "planned"
    if value.get("route_source"):
        return "already_routed"
    if value.get("questions"):
        return "needs_classification"
    if value.get("providers"):
        return "held"
    if value.get("reason") or value.get("detail"):
        return "no_candidates"
    return None


def last_plan(bindings: Mapping[str, Any] | None) -> tuple[str | None, Mapping[str, Any]]:
    """The latest plan a run saved: ``(outcome, value)``; ``(None, {})`` when none."""
    for name in PLAN_BINDINGS:
        value = (bindings or {}).get(name)
        if isinstance(value, Mapping):
            return plan_outcome(value), value
    return None, {}


def _reason(code: str, detail: str, ref: str | None) -> dict[str, Any]:
    return {"code": code, "detail": detail, "ref": ref}


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds}s"
    if seconds < 7200:
        return f"{seconds // 60}m"
    return f"{seconds // 3600}h"


def _names(values: Iterable[Any]) -> str:
    return ", ".join(str(value) for value in values if value) or "unknown"


def unbound_reason(project_id: str, why: str = "") -> dict[str, Any]:
    """No router routes the project: none bound, or a missing or non-routing one."""
    return _reason(
        "router_unbound",
        (why or f"project '{project_id}' is bound to no router")
        + "; nothing routes its tasks until it is bound to a routing playbook "
        "(`aq doctor --check routing.bypassed --fix` binds the default router)",
        project_id,
    )


def not_ready_reason(project_id: str, router: str, why: str = "") -> dict[str, Any]:
    """The bound router has no enabled activation that grants ``task_route_apply``."""
    return _reason(
        "router_not_ready",
        f"router '{router}' of project '{project_id}' is not ready"
        + (f": {why}" if why else "")
        + "; no task of the project is routed and this one waits",
        router,
    )


def router_reason(
    task_id: str,
    *,
    router: str,
    run: Mapping[str, Any] | None,
    emitted_at: float | None,
    now: float,
) -> dict[str, Any]:
    """The explain reason for a task a ready router still owes a route.

    *run* is the router's newest run for the task (``None`` when there is
    none), *emitted_at* when the cascade last emitted ``task.route_needed``
    for it (``None`` when it has not since the daemon started).
    """
    if run is not None:
        run_id = str(run.get("run_id") or "")
        lifecycle = str(run.get("lifecycle") or "")
        outcome, plan = last_plan(run.get("bindings"))
        if lifecycle in FAILED_LIFECYCLES:
            if outcome == "no_candidates":
                why = str(plan.get("reason") or "no_worker_candidates")
                detail = str(plan.get("detail") or "")
                return _reason(
                    "route_no_candidates",
                    f"router '{router}' run {run_id} found no worker candidate: {why}"
                    + (f" ({detail})" if detail else "")
                    + "; it asks again every cascade until the policy, the project's "
                    "preferred provider or the fleet changes",
                    why,
                )
            error = str(run.get("error") or run.get("error_code") or "").strip()
            return _reason(
                "route_failed",
                f"router '{router}' run {run_id} {lifecycle}"
                + (f": {error}" if error else "")
                + "; the router is asked again on the next route_needed emission",
                run_id,
            )
        if lifecycle == "completed" and outcome == "held":
            providers = plan.get("providers") or [
                candidate.get("provider")
                for candidate in plan.get("candidates") or ()
                if isinstance(candidate, Mapping)
            ]
            return _reason(
                "route_held",
                f"router '{router}' run {run_id} held the task: every candidate is on a "
                f"provider that cannot launch now ({_names(providers)}); it routes when "
                "one recovers",
                _names(providers),
            )
        if lifecycle in LIVE_LIFECYCLES:
            return _reason(
                "awaiting_route",
                f"router '{router}' run {run_id} is {lifecycle} for this task",
                run_id,
            )
    if emitted_at is None:
        detail = (
            f"no route yet: the cascade asks router '{router}' (task.route_needed) once "
            "the task is otherwise eligible to run"
        )
    else:
        detail = (
            f"no route yet: task.route_needed was last emitted {_ago(now - emitted_at)} "
            f"ago for router '{router}'"
        )
        if run is not None:
            detail += f"; its last run {run.get('run_id')} wrote no route"
    return _reason("awaiting_route", detail, task_id)


__all__ = [
    "FAILED_LIFECYCLES",
    "LIVE_LIFECYCLES",
    "PLAN_BINDINGS",
    "last_plan",
    "not_ready_reason",
    "plan_outcome",
    "router_reason",
    "unbound_reason",
]
