"""Token budget management for fair resource allocation across projects.

``BudgetManager`` holds the configured fleet-wide daily token budget and the
arithmetic for fair-share allocation: target token ratios from per-project
credit weights, and how far each project's actual usage deviates from its
target (the "deficit score").  Keeping agent time proportional to credit
weights over rolling windows is what stops a bursty project from starving the
others.

The orchestrator owns one instance (``Orchestrator.budget``).  It is the
single source of truth for the global budget: the config-reload hook writes
``global_budget`` here and ``Orchestrator._schedule`` reads it back into every
``SchedulerState``, so a hot edit of ``global_token_budget_daily`` reaches the
scheduler through this object.  The ratio/deficit helpers are not on that
path -- ``Scheduler.schedule`` is a pure function over its snapshot and
recomputes the same arithmetic inline in its sort key.

See docs/specs/scheduler-and-budget.md for the full specification.
"""

from __future__ import annotations


class BudgetManager:
    # ``__slots__`` is the ratchet for the bug this class shipped with for
    # months: the orchestrator's config-reload hook assigned
    # ``_global_budget``, a name nothing reads, so a hot reload of
    # ``global_token_budget_daily`` silently never arrived.  Any misspelled
    # attribute is now an immediate AttributeError instead of dead state.
    __slots__ = ("global_budget",)

    def __init__(self, global_budget: int | None = None):
        self.global_budget = global_budget

    def calculate_target_ratios(self, weights: dict[str, float]) -> dict[str, float]:
        total = sum(weights.values())
        if total == 0:
            return {}
        return {pid: w / total for pid, w in weights.items()}

    def calculate_deficits(
        self, weights: dict[str, float], usage: dict[str, int]
    ) -> dict[str, float]:
        targets = self.calculate_target_ratios(weights)
        total_usage = sum(usage.values())
        if total_usage == 0:
            return dict(targets)
        result = {}
        for pid, target in targets.items():
            actual = usage.get(pid, 0) / total_usage
            result[pid] = target - actual
        return result

    def is_global_budget_exhausted(self, total_used: int) -> bool:
        if self.global_budget is None:
            return False
        return total_used >= self.global_budget

    def is_project_budget_exhausted(self, project_used: int, budget_limit: int | None) -> bool:
        if budget_limit is None:
            return False
        return project_used >= budget_limit
