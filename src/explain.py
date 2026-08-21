"""Explain — reason codes for why a task isn't running (WG-4).

Implements docs/specs/design/work-graph.md §9 and
docs/specs/implementation/work-graph.md §6.3.  Two sources of reasons:

* **Graph reasons** (persistent, from the DB): incomplete blocking deps,
  open gates, ``hold:*`` labels.  These come from queries in
  :mod:`src.database.queries.task_queries` and
  :mod:`src.database.queries.gate_queries`.
* **Capacity reasons** (transient, from the last scheduler tick): no idle
  agent, workspace locked, budget exhausted, provider cooldown.  These
  come from :func:`build_capacity_reasons` reading the orchestrator's
  cached ``SchedulerState``.

Callers assemble both lists and return them from ``_cmd_explain_task``,
graph reasons first (they are the persistent story) then capacity
(transient).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, TypedDict

from src.models import AgentState, ProjectStatus

if TYPE_CHECKING:  # pragma: no cover
    from src.scheduler import SchedulerState


__all__ = ["Reason", "build_capacity_reasons"]


class Reason(TypedDict):
    """One reason a task isn't ready to run.

    ``code`` is a stable machine-readable identifier — one of
    ``blocked_dependency``, ``blocked_gate``, ``no_idle_agent``,
    ``workspace_locked``, ``budget_exhausted``, ``rate_limited``,
    ``held``, ``project_paused``.  ``detail`` is a human string.
    ``ref`` names the specific entity (task id, gate id, workspace id,
    provider id) when one applies, else ``None``.
    """

    code: str
    detail: str
    ref: str | None


def build_capacity_reasons(
    task,
    state: "SchedulerState",
    workspace_counts: dict[str, int],
    idle_by_project: dict[str, int],
) -> list[Reason]:
    """Return the ordered capacity reasons why *task* wasn't scheduled.

    Mirrors :meth:`Orchestrator._describe_task_blocker` but returns
    structured :class:`Reason` dicts instead of one heuristic string.  The
    caller (``_cmd_explain_task``) concatenates graph reasons first, then
    these.
    """
    reasons: list[Reason] = []
    project = next((p for p in state.projects if p.id == task.project_id), None)
    if project is None:
        return [
            Reason(
                code="project_missing",
                detail=f"project '{task.project_id}' not found",
                ref=task.project_id,
            )
        ]
    if project.status != ProjectStatus.ACTIVE:
        reasons.append(
            Reason(
                code="project_paused",
                detail=f"project '{task.project_id}' status={project.status.value}",
                ref=task.project_id,
            )
        )
        return reasons

    pc = (state.project_constraints or {}).get(task.project_id)
    if pc and getattr(pc, "pause_scheduling", False):
        reasons.append(
            Reason(
                code="project_paused",
                detail=f"project '{task.project_id}' pause_scheduling=True",
                ref=task.project_id,
            )
        )
        return reasons

    avail = workspace_counts.get(task.project_id, 0)
    if avail == 0:
        reasons.append(
            Reason(
                code="workspace_locked",
                detail=f"no available workspace on project '{task.project_id}'",
                ref=task.project_id,
            )
        )
    idle = idle_by_project.get(task.project_id, 0)
    if idle == 0:
        reasons.append(
            Reason(
                code="no_idle_agent",
                detail=f"no idle agent on project '{task.project_id}'",
                ref=task.project_id,
            )
        )
    if state.global_budget is not None and state.global_tokens_used >= state.global_budget:
        reasons.append(
            Reason(
                code="budget_exhausted",
                detail=(
                    f"global token budget exhausted "
                    f"({state.global_tokens_used}/{state.global_budget})"
                ),
                ref=None,
            )
        )
    for agent in state.agents:
        if (
            agent.state == AgentState.IDLE
            and getattr(agent, "project_id", None) == task.project_id
        ):
            cool = (state.provider_cooldowns or {}).get(agent.profile_id, 0)
            if cool > state.now:
                reasons.append(
                    Reason(
                        code="rate_limited",
                        detail=(
                            f"provider '{agent.profile_id}' in cooldown for "
                            f"{int(cool - state.now)}s"
                        ),
                        ref=agent.profile_id,
                    )
                )
    return reasons
